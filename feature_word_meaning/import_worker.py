"""Private Cloud Tasks worker factory, with explicit authenticated callbacks."""
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .import_engine import EngineError, LearningReviewRequired, SourceChanged, WorkBusy


class WorkItem(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: Literal["prepare", "batch", "finalize"]
    job_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,160}$")
    batch_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,160}$")
    generation: int = Field(ge=0)


def google_identity_verifier(*, audience, allowed_service_accounts):
    """Cloud Run IAM remains required; additionally bind token audience and caller."""
    accounts = frozenset(allowed_service_accounts)
    if not audience.startswith("https://") or not accounts:
        raise ValueError("Worker identity configuration is required")
    def verify(request):
        authorization = request.headers.get("authorization", "")
        if not authorization.startswith("Bearer "):
            raise HTTPException(401, "작업자 인증이 필요합니다.")
        from google.auth.transport.requests import Request as GoogleRequest
        from google.oauth2 import id_token
        try:
            claims = id_token.verify_oauth2_token(authorization[7:], GoogleRequest(), audience=audience)
        except Exception:
            raise HTTPException(401, "작업자 인증을 확인할 수 없습니다.") from None
        if (claims.get("iss") not in {"accounts.google.com", "https://accounts.google.com"}
                or claims.get("email") not in accounts or claims.get("email_verified") is not True):
            raise HTTPException(403, "이 작업자를 실행할 권한이 없습니다.")
    return verify


def create_worker(engine, dispatcher, *, verify_identity, prepare_handler=None, reconcile_handler=None):
    """No implicit public mode. Offline tests inject an explicit fake verifier."""
    if not callable(verify_identity):
        raise ValueError("Authenticated worker verifier is required")
    app = FastAPI(title="Melody learning worker", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    def health():
        return {"ok": True, "worker": "learning-v2"}

    @app.post("/tasks/learning")
    def work(item: WorkItem, request: Request):
        verify_identity(request)
        if (item.kind == "batch") != (item.batch_id is not None):
            raise HTTPException(422, "학습 묶음 요청을 확인해 주세요.")
        if item.kind == "prepare":
            if prepare_handler is None:
                raise HTTPException(503, "원문 준비 작업자가 설정되지 않았습니다.")
            try:
                return prepare_handler(item.model_dump())
            except Exception:
                return JSONResponse({"state": "retry", "message": "원문 준비 작업을 다시 확인합니다."},
                                    status_code=503, headers={"Retry-After": "30"})
        try:
            result = engine.run_batch(item.job_id, item.batch_id) if item.kind == "batch" else engine.finalize(item.job_id)
            return result
        except KeyError:
            raise HTTPException(404, "학습 작업을 찾을 수 없습니다.") from None
        except (EngineError, ValueError) as error:
            status = engine.status(item.job_id)
            if status["state"] in {"failed", "superseded", "cancelled"} or (status["state"] == "needs_review" and isinstance(error, (SourceChanged, LearningReviewRequired))):
                # A durable terminal outcome is acknowledged, never reported as
                # fully generated. No further queue retries can improve it.
                return {"state": status["state"], "error_code": status["error_code"]}
            return JSONResponse({"state": "retry", "message": "저장된 작업에서 재시도합니다."},
                                status_code=503, headers={"Retry-After": "30"})
        except Exception:
            return JSONResponse({"state": "retry", "message": "작업 연결을 다시 확인합니다."},
                                status_code=503, headers={"Retry-After": "30"})

    @app.post("/tasks/reconcile")
    def reconcile(request: Request):
        verify_identity(request)
        return reconcile_handler() if reconcile_handler is not None else engine.reconcile(dispatcher)

    return app
