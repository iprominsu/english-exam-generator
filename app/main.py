import asyncio
import os
import tempfile
import uuid
import time
import re
import logging
from copy import deepcopy
from typing import Literal, Optional
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi import Path as ApiPath
from fastapi.responses import Response, HTMLResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from . import config, pipeline, sheets_io, validation, gpt_subscription, assets, vocab, operations, candidates, legacy_generator
from .errors import AppError, public_error
from .auth import BasicAuthMiddleware
from .google_clients import download_to_path, drive_service
from .worksheet_downloads import (DownloadItem, LABELS, create_download_router,
    find_existing_folder, load_feature_pdf, safe_filename, split_legacy_pdf)
from urllib.parse import quote

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")

app = FastAPI(title="부스터보카 시험지 생성기")
if os.environ.get("K_SERVICE") and not config.APP_BASIC_PASS:
    raise RuntimeError("Cloud Run requires APP_BASIC_PASS.")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# A separate worksheet kind keeps legacy questions and Drive links intact.
from feature_word_meaning.api import create_router
from feature_word_meaning.drive_sync import DriveSyncError
from feature_word_meaning.production import (create_production_service,
    OperatingDriveBackend, ProductionFeatureService)

word_meaning_service = create_production_service(Path(STATIC_DIR).parent / ".word-meaning-cache")
from feature_word_meaning.import_cloud import configure_production_imports
configure_production_imports(word_meaning_service)
app.state.word_meaning = word_meaning_service
app.include_router(create_router(word_meaning_service))


class _ExistingDownloadBackend(OperatingDriveBackend):
    # Normal generation retains its setup behavior. Exports fail closed if any
    # configured folder is missing, including on the first request after boot.
    _folder = staticmethod(find_existing_folder)


_download_feature_service = ProductionFeatureService(
    word_meaning_service.store.root, backend=_ExistingDownloadBackend())
_download_feature_service.lock = word_meaning_service.lock


@app.get("/word-meaning", response_class=HTMLResponse)
def word_meaning_index():
    return assets.versioned_html((Path(__file__).resolve().parents[1] / "feature_word_meaning" / "static" / "index.html").read_text(encoding="utf-8"))


class ExistingVocabularyBody(BaseModel):
    file_id: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")


@app.post("/api/word-meaning/imports/existing", status_code=202)
async def import_existing_vocabulary(body: ExistingVocabularyBody):
    try:
        return await run_in_threadpool(word_meaning_service.import_existing, body.file_id)
    except Exception as error:
        raise public_error(error)


VOCAB_FOLDER_NAME = "어휘 파일"
_READ_ONLY_POST_PATHS = frozenset({
    "/api/worksheet-downloads",
    "/api/word-meaning/selection",
    "/api/word-meaning/selection-bulk",
})

mutation_lock = asyncio.Lock()
_worksheet_cache = {}
_worksheet_cache_epoch = 0
_worksheet_list_lock = asyncio.Lock()


def _invalidate_worksheet_cache():
    global _worksheet_cache_epoch
    _worksheet_cache_epoch += 1
    _worksheet_cache.clear()


@app.middleware("http")
async def guard_requests(request, call_next):
    if request.scope.get("path") in assets.STATIC_URLS and request.query_params.get("v") not in (None, assets.BUILD_ID):
        return JSONResponse({"detail": "새 버전이 배포됐습니다. 페이지를 새로고침해 주세요."}, status_code=409)
    if (request.method not in {"POST", "PUT", "PATCH", "DELETE"}
            or (request.method == "POST" and request.url.path in _READ_ONLY_POST_PATHS)):
        return await call_next(request)
    if mutation_lock.locked():
        return JSONResponse({"detail": "다른 저장 또는 생성 작업이 진행 중입니다."}, status_code=409)
    async with mutation_lock:
        _invalidate_worksheet_cache()
        try:
            return await call_next(request)
        finally:
            _invalidate_worksheet_cache()


app.add_middleware(BasicAuthMiddleware)


@app.middleware("http")
async def release_headers(request, call_next):
    response = await call_next(request)
    versioned_asset = request.scope.get("path") in assets.STATIC_URLS
    asset_version = request.query_params.get("v")
    response.headers["X-App-Build"] = assets.BUILD_ID
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Content-Security-Policy"] = "frame-ancestors 'self'"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Cache-Control"] = (
        "private, max-age=31536000, immutable"
        if versioned_asset and asset_version == assets.BUILD_ID and request.method in {"GET", "HEAD"} and response.status_code == 200
        else "private, no-store"
    )
    return response


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/")
def index():
    return HTMLResponse(assets.index_html())


@app.get("/about")
def about():
    return HTMLResponse(assets.versioned_html(Path(STATIC_DIR, "about.html").read_text(encoding="utf-8")))


@app.get("/privacy")
def privacy():
    return HTMLResponse(assets.versioned_html(Path(STATIC_DIR, "privacy.html").read_text(encoding="utf-8")))


# ------------------------------------------------------------
# 드라이브 CTA
# ------------------------------------------------------------
@app.get("/api/drive-folder")
async def drive_folder():
    def _get():
        folder_id = sheets_io.get_or_create_folder(config.PROJECT_FOLDER)
        meta = drive_service().files().get(fileId=folder_id, fields="webViewLink").execute()
        about = drive_service().about().get(fields="user").execute()
        email = about.get("user", {}).get("emailAddress")
        return {"folder_id": folder_id, "web_link": meta["webViewLink"], "email": email}

    try:
        return await run_in_threadpool(_get)
    except Exception as e:
        raise public_error(e)


async def _download_vocab(vocab_file_id: str) -> str:
    """구형 Excel 원본을 임시 파일로 내려받는다(구형 시험지 재생성·생성 호환용)."""
    fd, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(fd)
    try:
        await run_in_threadpool(download_to_path, vocab_file_id, path)
    except BaseException:
        os.unlink(path)
        raise
    return path


# ------------------------------------------------------------
# 시험지 목록
# ------------------------------------------------------------
@app.get("/api/worksheets")
async def worksheets():
    try:
        # One scan serves concurrent refreshes; a scan costs two Sheets reads
        # per historical file even when Drive holds many Day folders.
        async with _worksheet_list_lock:
            if _worksheet_cache and time.monotonic() - _worksheet_cache["time"] < 60:
                return _worksheet_cache["data"]
            epoch = _worksheet_cache_epoch
            data = await pipeline.list_worksheets()
            # A concurrent save can finish after this scan began. Do not cache
            # its older list after that save has invalidated the cache.
            if epoch == _worksheet_cache_epoch and not mutation_lock.locked():
                _worksheet_cache.update(time=time.monotonic(), data=data)
            return data
    except Exception as e:
        raise public_error(e)


@app.get("/api/worksheet/{day}/{version}")
async def get_worksheet(day: int, version: int):
    try:
        return await pipeline.get_worksheet(day, version)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise public_error(e)


def _read_saved_legacy_pdf(day: int, version: int):
    """저장된 버전의 PDF만 읽고 렌더링·수정·발행은 하지 않는다."""
    ctx, _, loaded = pipeline._get_ws_loaded(day, config.EXAM_COUNT_DEFAULT, version, create_project=False)
    ctx = sheets_io.version_ctx(ctx, pipeline.pick_set(loaded, version))
    name = sheets_io.pdf_name_for(ctx["title"], version)
    meta = sheets_io.find_file(name, ctx["folder_id"])
    if not meta:
        raise HTTPException(404, "저장된 PDF를 찾을 수 없습니다.")
    return name, drive_service().files().get_media(fileId=meta["id"]).execute()


def _load_saved_download(item, kind):
    if item.kind != "standard":
        return load_feature_pdf(_download_feature_service, item, kind)
    try:
        filename, data = _read_saved_legacy_pdf(item.day, item.version)
    except ValueError as error:
        raise HTTPException(404, str(error)) from None
    return f"{filename[:-4]} {LABELS[kind]}.pdf", split_legacy_pdf(data, kind)


app.include_router(create_download_router(_load_saved_download))


@app.get("/api/worksheet/{day}/{version}/pdf")
async def worksheet_pdf(day: int = ApiPath(..., ge=1, le=100000), version: int = ApiPath(..., ge=1, le=100000), kind: Literal["student", "answer", "combined"] = "combined", download: bool = False):
    try:
        filename, data = await run_in_threadpool(_load_saved_download,
            DownloadItem(kind="standard", day=day, version=version), kind)
    except HTTPException:
        raise
    except Exception as error:
        raise public_error(error) from None
    disposition = "attachment" if download else "inline"
    return Response(data, media_type="application/pdf", headers={
        "Content-Disposition": f'{disposition}; filename="worksheet-day{day}-v{version}.pdf"; filename*=utf-8\'\'{quote(safe_filename(filename), safe="")}',
        "Cache-Control": "private, no-store",
    })


class QuestionCore(BaseModel):
    no: Optional[int] = None
    type: str
    instruction: str
    answer: int = Field(strict=True, ge=1, le=5)
    explanation: str = ""
    payload: dict
    set: Optional[int] = None


class CandidateIn(QuestionCore):
    candidate_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9-]+$")


class QuestionIn(QuestionCore):
    alternatives: Optional[list[CandidateIn]] = Field(default=None, min_length=1, max_length=10)
    candidate_selected: int = Field(default=0, ge=0, le=9, strict=True)
    candidate_token: Optional[str] = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class SaveBody(BaseModel):
    questions: list[QuestionIn] = Field(min_length=1, max_length=50)


class RegenerateBody(BaseModel):
    request_id: str = Field(pattern=r"^[a-zA-Z0-9-]{20,80}$")
    candidate_token: str = Field(pattern=r"^[a-f0-9]{64}$")
    question: QuestionCore
    provider: Literal["script", "gpt", "gemini"] = "script"
    reasoning: Literal["low", "medium", "high", "xhigh"] = config.GPT_REASONING_DEFAULT


def _regeneration_vocab_id(source_id):
    missing = AppError("이 시험지의 원본 어휘 파일을 찾을 수 없습니다. 드라이브 휴지통에서 원본을 복원한 뒤 다시 시도해 주세요.", 422, "source_missing")
    if not source_id:
        raise missing
    try:
        _vocab_meta(source_id)
        return source_id
    except HTTPException as error:
        if error.status_code != 422:
            raise
    # A deleted duplicate may have an active byte-identical copy. Never substitute
    # a book based only on its name, Day number, or ordering in the upload list.
    parent = sheets_io.get_or_create_folder(VOCAB_FOLDER_NAME, sheets_io.get_or_create_folder(config.PROJECT_FOLDER))
    original = drive_service().files().get(fileId=source_id, fields="trashed,parents,md5Checksum").execute(num_retries=2)
    if not original.get("trashed") or parent not in original.get("parents", []) or not original.get("md5Checksum"):
        raise missing
    page = None
    while True:
        result = drive_service().files().list(q=f"'{parent}' in parents and trashed=false", fields="nextPageToken,files(id,name,md5Checksum)", pageSize=1000, pageToken=page).execute(num_retries=2)
        for file in result.get("files", []):
            if file.get("md5Checksum") == original["md5Checksum"] and file["name"].lower().endswith(".xlsx"):
                _vocab_meta(file["id"])
                return file["id"]
        page = result.get("nextPageToken")
        if not page:
            raise missing


@app.post("/api/worksheet/{day}/{version}/questions/{question_index}/regenerate")
async def regenerate_question(day: int, version: int, question_index: int, body: RegenerateBody):
    params = {"kind": "question_candidate", "day": day, "version": version,
              "question_index": question_index, **body.model_dump(exclude={"request_id"})}
    path = None
    record = None
    started_here = False
    try:
        record = await run_in_threadpool(operations.load, body.request_id)
        operations.check_request(record, params)
        if record and record["state"] == "done":
            return record["result"]
        if not record or not record.get("questions"):
            worksheet = await pipeline.get_worksheet(day, version)
            if not 0 <= question_index < len(worksheet["questions"]):
                raise HTTPException(422, "문항 번호를 확인해 주세요.")
            original = worksheet["questions"][question_index]
            candidates.check_token(original, body.candidate_token)
            previous = candidates.items(original)
            if len(previous) >= 10:
                raise AppError("더 이상 생성할 수 없습니다.", 409, "candidate_limit")
            current = body.question.model_dump()
            validation.validate_questions([current], check_context=False)
            if current["type"] != original["type"]:
                raise HTTPException(422, "기존 문항 유형을 확인해 주세요.")
            script = original.get('payload', {}).get('_script') or {}
            current['payload']['_script'] = deepcopy(script)
            generation_request_id = script.get('generation_request_id', '')
            if generation_request_id:
                if not re.fullmatch(r'[a-zA-Z0-9-]{20,80}', generation_request_id):
                    raise AppError('최초 생성 작업 정보를 확인해 주세요.', 422, 'source_missing')
                source_record = await run_in_threadpool(operations.load, generation_request_id)
                if not source_record or not source_record.get('bank_snapshot'):
                    raise AppError('최초 단어장 스냅샷을 찾을 수 없습니다.', 422, 'source_missing')
                bank, unit_ids = source_record['bank_snapshot'], source_record['unit_ids']
            else:
                source_id = worksheet.get("source_file_id")
                try:
                    source_id = await run_in_threadpool(_regeneration_vocab_id, source_id)
                except Exception as error:
                    raise AppError("이 시험지의 원본 어휘 파일을 찾을 수 없습니다. 드라이브 휴지통에서 원본을 복원한 뒤 다시 시도해 주세요.", 422, "source_missing") from error
                path = await _download_vocab(source_id)
                rows = await run_in_threadpool(vocab.load_range, path, day, worksheet.get("day_end", day))
                bank = vocab.to_bank(rows, day, source={'id': source_id, 'name': worksheet.get('source_name', '')})
                unit_ids = [unit['id'] for unit in bank['units']]
                legacy_generator.reuse_saved_material(bank, original)
            record = record or {"request_id": body.request_id, "params": params}
            record["state"] = "generating"
            await run_in_threadpool(operations.save, record)
            started_here = True
            question = await run_in_threadpool(legacy_generator.regenerate, bank, unit_ids, current, previous,
                request_id=body.request_id, generation_request_id=generation_request_id)
            question['payload']['_script']['unit_labels'] = [unit['label'] for unit in bank['units'] if unit['id'] in unit_ids]
            validation.validate_questions([question], generated=True)
            record.update(state="generated", questions=[question])
            await run_in_threadpool(operations.save, record)
        result = await run_in_threadpool(pipeline.append_candidate, day, version, question_index,
                                         body.candidate_token, record["questions"][0], body.request_id)
        record.update(state="done", result={**result, "request_id": body.request_id})
        await run_in_threadpool(operations.save, record)
        return record["result"]
    except (AppError, gpt_subscription.SubscriptionError, ValueError) as error:
        if started_here and record and record.get("state") == "generating":
            record.update(state="failed")
            await run_in_threadpool(operations.save, record)
        if isinstance(error, gpt_subscription.SubscriptionError):
            raise HTTPException(error.status, {"message": str(error), "code": "subscription"})
        # DriveSyncError는 ValueError의 하위 클래스이므로 일반 형식 오류 422 대신 public_error로 전달한다.
        if isinstance(error, ValueError) and not isinstance(error, DriveSyncError):
            raise HTTPException(422, {"message": "문항 또는 원본 어휘 형식을 확인해 주세요.", "code": "validation"})
        raise public_error(error)
    except HTTPException:
        raise
    except Exception as error:
        if record and record.get("questions"):
            raise HTTPException(503, {"message": "문항은 생성됐지만 후보 저장을 완료하지 못했습니다. 다시 누르면 같은 후보 저장부터 이어갑니다.", "code": "save_pending"})
        raise public_error(error)
    finally:
        if path:
            os.unlink(path)


@app.put("/api/worksheet/{day}/{version}")
async def save_worksheet(day: int, version: int, body: SaveBody):
    edited = [q.model_dump() for q in body.questions]
    try:
        for i, q in enumerate(edited, 1):
            q["no"] = i
        validation.validate_questions(edited)
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        raise HTTPException(400, f"문항 형식을 확인하세요: {e}")
    try:
        return await pipeline.save_worksheet(day, version, edited)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise public_error(e)


class WorksheetDeleteBody(BaseModel):
    model_config = {'extra':'forbid'}
    mode: Literal['soft', 'permanent']
    token: str = Field(min_length=64, max_length=64, pattern=r'^[a-f0-9]{64}$')


@app.delete("/api/worksheet/{day}/{version}")
async def delete_worksheet(day: int, version: int, body: 'WorksheetDeleteBody'):
    try:
        return await pipeline.delete_worksheet(day, version, mode=body.mode, token=body.token)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise public_error(e)


@app.post("/api/worksheet/{day}/{version}/render")
async def rebuild_pdf(day: int, version: int):
    try:
        ctx, _, loaded = await run_in_threadpool(pipeline._get_ws_loaded, day, config.EXAM_COUNT_DEFAULT, version)
        questions = pipeline.pick_set(loaded, version)
        validation.validate_questions(questions)
        ctx = sheets_io.version_ctx(ctx, questions)
        return await pipeline._render_and_upload(questions, ctx, version)
    except ValueError as e:
        raise HTTPException(404, str(e))
    except Exception as e:
        raise public_error(e)


# ------------------------------------------------------------
# 생성 (단일 / 일괄)
# ------------------------------------------------------------
class GenerateBody(BaseModel):
    request_id: Optional[str] = Field(default=None, pattern=r"^[a-zA-Z0-9-]{20,80}$")
    day: Optional[int] = Field(default=None, ge=1, le=365, strict=True)
    day_end: Optional[int] = Field(default=None, ge=1, le=365, strict=True)
    vocab_file_id: Optional[str] = Field(default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    bank_id: Optional[str] = Field(default=None, min_length=1, max_length=200, pattern=r"^[A-Za-z0-9_-]+$")
    unit_ids: Optional[list[str]] = Field(default=None, min_length=1, max_length=1000)
    seed: Optional[int | str] = None
    question_types: Optional[list[str]] = Field(default=None, min_length=1, max_length=8)
    allow_partial: bool = Field(default=False, strict=True)
    provider: Literal["script", "gpt", "gemini"] = "script"
    reasoning: Literal["low", "medium", "high", "xhigh"] = config.GPT_REASONING_DEFAULT
    count: int = Field(default=config.EXAM_COUNT_DEFAULT, ge=1, le=20, strict=True)


@app.get('/api/legacy/banks/{bank_id}/capabilities')
async def legacy_capabilities(bank_id: str, unit_ids: list[str] = Query(...)):
    try:
        bank = await run_in_threadpool(word_meaning_service.get, 'banks', bank_id)
        legacy_generator.validate_selection(bank, unit_ids)
        snapshot, preparation = await run_in_threadpool(
            word_meaning_service.legacy_selection_snapshot, bank, unit_ids)
        result = await run_in_threadpool(legacy_generator.capabilities, snapshot, unit_ids)
        if not preparation.get('selection_ready', True):
            result.update(available_types=[], unavailable_types=list(legacy_generator.TYPES),
                          notice=preparation['message'])
        return {**result, **preparation}
    except Exception as error:
        raise public_error(error)


@app.post("/api/generate")
async def generate(body: GenerateBody):
    if bool(body.bank_id) == bool(body.vocab_file_id) or (body.bank_id and not body.unit_ids) or (body.vocab_file_id and body.day is None):
        raise HTTPException(422, {'message': '단어장과 챕터를 선택해 주세요.', 'code': 'invalid_selection'})
    day = body.day or 1
    end = body.day_end if body.day_end is not None else day
    if end < day:
        raise HTTPException(422, {"message": "끝 Day는 시작 Day보다 작을 수 없습니다.", "fields": {"day_end": "시작 Day 이후를 선택해 주세요."}})
    request_id = body.request_id or str(uuid.uuid4())
    params = body.model_dump(exclude={"request_id"}, exclude_none=True)
    if not body.bank_id:
        params.pop('allow_partial', None)  # Preserve historical checkpoint keys.
    if end == day:
        params.pop("day_end", None)
    if body.provider != "gpt":
        params.pop("reasoning", None)
    record = await run_in_threadpool(operations.load, request_id)
    operations.check_request(record, params)
    if record and record["state"] == "done":
        return record["result"]
    vocab_path, bank, unit_ids = None, None, None
    try:
        if record and record.get("questions") and record["questions"][0].get("source_name"):
            # The generated rows already contain the frozen source needed to resume saving.
            source = None
            if body.bank_id:
                day = end = 1
        elif body.bank_id:
            unit_ids = list(body.unit_ids)
            if record and record.get('bank_snapshot'):
                # Existing requests keep their verified, frozen source, even
                # when a later bank edit invalidates new exam preparation.
                bank = deepcopy(record['bank_snapshot'])
            else:
                bank = await run_in_threadpool(word_meaning_service.get, 'banks', body.bank_id)
                legacy_generator.validate_selection(bank, unit_ids)
                bank, preparation = await run_in_threadpool(
                    word_meaning_service.legacy_selection_snapshot, bank, unit_ids)
                if not preparation.get('selection_ready', True):
                    raise HTTPException(409, {'code': 'learning_not_ready', **preparation})
            legacy_generator.validate_selection(bank, unit_ids)
            labels = [unit['label'] for unit in bank['units'] if unit['id'] in unit_ids]
            # Day/version remains a storage identity; actual chapter labels are
            # persisted separately and never inferred from these numeric keys.
            day = end = 1
            name = bank.get('title') or bank.get('source', {}).get('filename') or '단어장'
            source = {'id': bank.get('source', {}).get('original_id', ''), 'name': name,
                      'exam_title': name + ' ' + ', '.join(labels)}
        else:
            source = await run_in_threadpool(_vocab_meta, body.vocab_file_id)
            vocab_path = await _download_vocab(body.vocab_file_id)
            days = await run_in_threadpool(vocab.available_days, vocab_path)
            if any(d not in days for d in range(day, end + 1)):
                raise HTTPException(422, {"message": "선택한 파일에 범위 내 모든 Day가 있어야 합니다.", "fields": {"day_end" if end != day else "day": "파일에 있는 연속된 Day 범위를 선택해 주세요."}})
        if not record:
            record = {"request_id": request_id, "params": params}
        if not record.get("questions"):
            record["state"] = "generating"
            await run_in_threadpool(operations.save, record)
        result = await pipeline.generate_and_render(day, vocab_path, body.count,
            source=source, record=record, **({"day_end": end} if end != day else {}),
            bank=bank, unit_ids=unit_ids, seed=body.seed, question_types=body.question_types, allow_partial=body.allow_partial)
        record.update(state="done", result={**result, "request_id": request_id})
        await run_in_threadpool(operations.save, record)
        return record["result"]
    except (AppError, gpt_subscription.SubscriptionError, ValueError) as e:
        if record and not record.get("questions"):
            record.update(state="failed", error={"message": "생성을 완료하지 못했습니다. 입력 또는 연결 상태를 확인한 뒤 재시도할 수 있습니다."})
            await run_in_threadpool(operations.save, record)
        if isinstance(e, gpt_subscription.SubscriptionError):
            raise HTTPException(e.status, {"message": str(e), "code": "subscription", "retryable": False})
        if isinstance(e, ValueError) and not isinstance(e, DriveSyncError):
            raise HTTPException(422, {"message": "생성된 문항 형식 또는 어휘 파일을 확인할 수 없습니다. 다시 시도해 주세요.", "retryable": True})
        raise public_error(e)
    except HTTPException:
        raise
    except Exception as e:
        # A checkpoint with questions resumes saving the same frozen result.
        if record and record.get("questions"):
            raise HTTPException(503, {"message": "문항 생성은 완료됐지만 저장을 마치지 못했습니다. 같은 작업을 재시도하면 저장부터 이어갑니다.", "code": "save_pending", "retryable": True})
        raise public_error(e)
    finally:
        if vocab_path:
            os.unlink(vocab_path)


@app.get("/api/operations/{request_id}")
async def operation_status(request_id: str):
    if not re.fullmatch(r"[a-zA-Z0-9-]{20,80}", request_id):
        raise HTTPException(400, "작업 번호를 확인해 주세요.")
    return operations.public(await run_in_threadpool(operations.load, request_id))


@app.exception_handler(AppError)
async def app_error_handler(request, exc):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request, exc):
    messages = {"count": "문항 수는 1~20 사이의 정수로 입력해 주세요.",
                "day": "선택한 파일에 있는 Day를 선택해 주세요.",
                "day_end": "선택한 파일에 있는 끝 Day를 선택해 주세요.",
                "provider": "선택한 모델을 현재 사용할 수 없습니다. 다른 모델을 선택해 주세요.",
                "reasoning": "추론 수준은 Light, Medium, High, XHigh 중에서 선택해 주세요.",
                "vocab_file_id": "유효한 어휘 파일을 선택해 주세요."}
    if request.url.path.startswith("/api/word-meaning/"):
        messages["count"] = "문항 수는 양의 정수로 입력하거나 전체 출제를 선택해 주세요."
    fields = {str(e["loc"][-1]): messages.get(str(e["loc"][-1]), "입력 형식을 확인해 주세요.") for e in exc.errors()}
    return JSONResponse({"detail": {"code": "validation", "message": " / ".join(dict.fromkeys(fields.values())),
                                  "fields": fields, "retryable": False}}, status_code=422)


@app.exception_handler(Exception)
async def unexpected_error_handler(request, exc):
    logging.getLogger(__name__).error("Request failed: %s %s", request.url.path, type(exc).__name__)
    error = public_error(exc)
    return JSONResponse({"detail": error.detail}, status_code=error.status_code)


def _vocab_meta(file_id):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", file_id):
        raise HTTPException(422, "등록된 어휘 파일을 선택해 주세요.")
    project = sheets_io.get_or_create_folder(config.PROJECT_FOLDER)
    parent = sheets_io.get_or_create_folder(VOCAB_FOLDER_NAME, project)
    meta = drive_service().files().get(fileId=file_id, fields="id,name,parents,trashed,modifiedTime,mimeType").execute(num_retries=2)
    if meta.get("trashed") or parent not in meta.get("parents", []) or not meta["name"].lower().endswith(".xlsx") or meta.get("mimeType") == "application/vnd.google-apps.folder":
        raise HTTPException(422, {"message": "등록된 어휘 파일을 선택해 주세요.", "fields": {"vocab_file_id": "유효한 파일을 선택해 주세요."}})
    return meta
