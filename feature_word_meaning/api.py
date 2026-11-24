"""단어장과 시험지 API 라우터, 오프라인 개발·검증용 앱 팩토리."""

from __future__ import annotations

import copy
import hashlib
import io
import threading
import time
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from pathlib import Path
from urllib.parse import quote
from typing import Literal

from fastapi import APIRouter, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from .storage import ConflictError, JsonStore, StoreError, now

PREFIX = "/api/word-meaning"
MAX_UPLOAD_BYTES = 64 * 1024 * 1024


def _check_upload_size(content):
    if not content or len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "빈 파일 또는 64MB를 초과하는 파일은 등록할 수 없습니다.")


class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str | None = Field(default=None, min_length=20, max_length=80, pattern=r"^[a-zA-Z0-9-]+$")
    bank_id: str
    worksheet_kind: Literal["word_meaning_test", "meaning_writing_test"] = "word_meaning_test"
    unit_ids: list[str] = Field(min_length=1)
    count: StrictInt | None = None
    shuffle: StrictBool = False
    allow_partial: StrictBool = False
    seed: StrictInt | None = None
    title: str | None = Field(default=None, max_length=160)
    model_options: dict = Field(default_factory=dict)


class BulkGenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str | None = Field(default=None, min_length=20, max_length=80, pattern=r"^[a-zA-Z0-9-]+$")
    bank_id: str
    worksheet_kind: Literal["word_meaning_test", "meaning_writing_test"] = "word_meaning_test"
    ranges: list[list[str]] = Field(min_length=1)
    count: StrictInt | None = None
    shuffle: StrictBool = False
    allow_partial: StrictBool = False
    seed: StrictInt | None = None
    title: str | None = Field(default=None, max_length=160)
    model_options: dict = Field(default_factory=dict)


class EntryBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt
    word: str = Field(min_length=1, max_length=300)
    meaning: str = Field(min_length=1, max_length=4000)
    status: Literal["ready", "review", "excluded"] | None = None
    review_reason: str | None = Field(default=None, max_length=1000)


class UnitBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt
    label: str = Field(min_length=1, max_length=160)
    status: Literal["ready", "review"] | None = None
    review_reason: str | None = Field(default=None, max_length=1000)


class WorksheetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt
    title: str | None = Field(default=None, min_length=1, max_length=160)
    questions: list[dict] | None = None


class ReplaceQuestionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt = Field(ge=1)
    request_id: str | None = Field(default=None, min_length=20, max_length=80, pattern=r"^[a-zA-Z0-9-]+$")


class DeleteWorksheetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    mode: Literal['soft', 'permanent']
    version: StrictInt


class DeleteBankBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt = Field(ge=1)
    mode: Literal['soft', 'permanent'] = 'soft'


class RenameBankBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt = Field(ge=1)
    title: str = Field(min_length=1, max_length=160)


class SourceReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt = Field(ge=1)
    mode: Literal['ai'] = 'ai'
    request_id: str = Field(min_length=20, max_length=80, pattern=r'^[a-zA-Z0-9-]+$')


class ReviewBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt
    question_ids: list[str] = Field(min_length=1)


class RegenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str | None = Field(default=None, min_length=20, max_length=80, pattern=r"^[a-zA-Z0-9-]+$")
    version: StrictInt
    model_options: dict = Field(default_factory=dict)


class CandidateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt
    candidate_id: str


class EntryChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    word: str = Field(min_length=1, max_length=300)
    meaning: str = Field(min_length=1, max_length=4000)
    status: Literal["ready", "review", "excluded"] | None = None
    review_reason: str | None = Field(default=None, max_length=1000)


class EntriesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt
    entries: list[EntryChange] = Field(min_length=1, max_length=200)
    reviewer: str = Field(default="사용자", min_length=1, max_length=80)


class UnitChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=100)
    label: str = Field(min_length=1, max_length=160)
    status: Literal["ready"] | None = None


class BankEditBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: StrictInt = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=160)
    units: list[UnitChange] = Field(default_factory=list, max_length=2000)
    entries: list[EntryChange] = Field(default_factory=list, max_length=10000)


class FeatureService:
    def __init__(self, root, provider=None, ocr_engine=None, import_provider=None):
        self.store = JsonStore(root)
        self.provider = provider
        self.import_provider = import_provider
        self.ocr_engine = ocr_engine
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="word-meaning")
        self.lock = threading.RLock()
        self.active = set()
        self.store.recover_jobs()

    def get(self, kind, record_id, *, allow_pending=False):
        try:
            value = self.store.get(kind, record_id)
            if kind == 'banks' and value.get('deleted_at'):
                raise KeyError(record_id)
            if kind == 'banks' and value.get('deletion_pending') and not allow_pending:
                raise HTTPException(409, '완전 삭제가 진행 중인 단어장입니다.')
            if kind == 'worksheets':
                if value.get('deleted_at'):
                    raise KeyError(record_id)
                if value.get('deletion_pending'):
                    raise HTTPException(409, '완전 삭제가 진행 중인 시험지입니다.')
            return value
        except (KeyError, StoreError):
            raise HTTPException(404, "항목을 찾을 수 없습니다.") from None

    def public_bank(self, bank):
        """Response-only state projection; raw records remain storage inputs."""
        if bank.get('construction', {}).get('engine_version') != 2:
            return bank
        coordinator = getattr(self, 'import_coordinator', None)
        if coordinator is not None:
            value = coordinator.public_bank(bank)
        else:
            value = copy.deepcopy(bank)
            value['construction'].update(publication_verified=False, publishing_status='pending', learning_complete=False)
        if 'registration_status' in value['construction']:
            value['registration_status'] = value['construction']['registration_status']
        return value

    def legacy_selection_snapshot(self, bank, unit_ids):
        """Read verified learning for a new exam without changing the bank."""
        policy = bank.get('construction', {}).get('preparation_policy')
        if not policy:
            # Older banks keep their capability notice and supported types.
            return bank, {}
        coordinator = getattr(self, 'import_coordinator', None)
        if policy == 'before_review_v1' and coordinator is not None and hasattr(coordinator, 'selection_snapshot'):
            return coordinator.selection_snapshot(bank, unit_ids)
        return copy.deepcopy(bank), {
            'selection_ready': False, 'preparation_state': 'unavailable',
            'blocking_unit_ids': list(unit_ids),
            'message': '학습자료 준비 상태를 확인할 수 없습니다. 잠시 후 다시 시도해 주세요.'}

    def worksheet_bank(self, worksheet):
        """Existing worksheets may still use a retained, unregistered bank."""
        if worksheet.get("generation_bank_snapshot"):
            return worksheet["generation_bank_snapshot"]
        try:
            return self.store.get("banks", worksheet["bank_id"])
        except (KeyError, StoreError):
            raise HTTPException(404, "시험지의 원본 단어장을 찾을 수 없습니다.") from None

    @contextmanager
    def bank_write_context(self, bank_id):
        guard = getattr(self, 'bank_write_guard', None)
        try:
            with guard(bank_id) if guard is not None else nullcontext():
                yield
        except ConflictError as error:
            raise HTTPException(409, str(error)) from None

    def assert_bank_write_ownership(self):
        """로컬 쓰기는 self.lock이 보호하고 클라우드 런타임은 리스 검사로 교체한다."""

    def delete_bank(self, bank_id, version, mode='soft'):
        try:
            self.store.path("banks", bank_id)
        except StoreError:
            raise HTTPException(422, "올바른 단어장 ID가 필요합니다.") from None
        with self.lock, self.bank_write_context(bank_id):
            try:
                bank = self.store.get("banks", bank_id)
            except KeyError:
                if mode == 'permanent':
                    return {"id": bank_id, "deleted": True, "mode": mode}
                raise HTTPException(404, "단어장을 찾을 수 없습니다.") from None
            if mode == 'soft' and bank.get('deletion_pending'):
                raise HTTPException(409, '완전히 삭제를 다시 시도해 주세요.')
            if mode == 'soft' and bank.get("deleted_at"):
                return {"id": bank_id, "deleted": True}
            if version != bank["version"] and not (bank.get('deletion_pending') and version == bank.get('deletion_version')):
                raise HTTPException(409, "단어장이 변경되었습니다. 목록을 새로고침해 주세요.")
            source_hash = bank.get("source", {}).get("sha256")
            for job in self.store.list("jobs"):
                payload = job.get("payload", {})
                importing = (job.get("kind") == "import" and
                             (job.get("result_id") == bank_id or
                              (source_hash and payload.get("sha256") == source_hash)))
                generating = (job.get("kind") == "generate" and
                              (payload.get("bank_id") == bank_id or
                               (job.get("bank_snapshot") or {}).get("id") == bank_id))
                status, active = job.get("status"), job["id"] in self.active
                review_only_failure = (job.get('source_review_of') == bank_id
                    and bank.get('construction', {}).get('import_job_id') != job['id']
                    and status in {'failed', 'interrupted'})
                replaced_failure = (job.get('result_id') == bank_id
                    and bank.get('construction', {}).get('import_job_id')
                    and bank['construction']['import_job_id'] != job['id']
                    and status in {'failed', 'interrupted'})
                if importing and ((status in {"queued", "running", "failed", "interrupted"} and not review_only_failure and not replaced_failure) or active):
                    raise HTTPException(409, "이 단어장의 등록 작업이 진행 중이거나 재시도를 기다리고 있습니다. 등록을 완료한 뒤 삭제해 주세요.")
                has_snapshot = bool(job.get("bank_snapshot")) and "selected_words" in job
                if generating and (status in {"queued", "running"} or active
                                   or (status in {"failed", "interrupted"} and not has_snapshot)):
                    raise HTTPException(409, "이 단어장의 시험지 생성 작업이 끝난 뒤 삭제해 주세요.")
            if mode == 'permanent':
                from .bank_deletion import permanently_delete_bank
                return permanently_delete_bank(self, bank)
            bank["deleted_at"] = now()
            try:
                self.store.put_metadata("banks", bank, expected_version=version)
            except ConflictError as error:
                raise HTTPException(409, str(error)) from None
            return {"id": bank_id, "deleted": True}

    def status(self):
        return {"model_connected": False, "available_providers": [], "generation_mode": "script", "generation_available": True, "storage": "local", "work_mode": "background", "feature": "word_meaning_test"}

    @staticmethod
    def script_params(params):
        """Accept stale browser model fields without using them or changing intent."""
        result = copy.deepcopy(params)
        result["model_options"] = {}
        result.setdefault("worksheet_kind", "word_meaning_test")
        if result["worksheet_kind"] == "meaning_writing_test":
            result["shuffle"] = True
        return result

    def ensure_source(self, record):
        source = record.get("source") or record["payload"]
        return self.store.root / "uploads" / source["stored_name"]

    def ensure_pdf(self, worksheet, kind):
        return self.store.root / "pdfs" / worksheet["id"] / f"{kind}.pdf"

    def publish_pdfs(self, worksheet, result):
        return worksheet

    def find_intent(self, kind, request_id, params):
        if request_id is None:
            return None, "j_" + uuid.uuid4().hex
        job_id = "j_" + hashlib.sha256((kind + ":" + request_id).encode()).hexdigest()[:32]
        try:
            existing = self.store.get("jobs", job_id)
        except KeyError:
            return None, job_id
        previous = existing.get("request_payload")
        if kind in {"generate", "regenerate"} and previous is not None:
            previous, params = self.script_params(previous), self.script_params(params)
        if existing["kind"] != kind or previous != params:
            raise HTTPException(409, "같은 작업 번호에 다른 입력을 사용할 수 없습니다.")
        return existing, job_id

    def new_import_fields(self):
        """Production may opt new jobs into an engine; resumed jobs stay intact."""
        return {}

    def read_job(self, job_id):
        return self.get("jobs", job_id)

    def public_import_state(self, value):
        result = {key: copy.deepcopy(value[key]) for key in
            ("engine_version", "preparation_policy", "registration_status", "source_status",
             "learning_status", "publish_status", "attention_at") if key in value}
        if value.get('registration_status') == 'complete' and value.get('result_id'):
            result['bank_id'] = value['result_id']
        return result

    def register_upload(self, filename, content, *, legacy_original_id=None):
        """Register bytes through one deduplicated path for uploads and existing Drive files."""
        self.assert_new_import_allowed()
        filename = Path((filename or "").replace("\\", "/")).name
        extension = Path(filename).suffix.lower()
        if extension not in {".pdf", ".xlsx"}:
            raise HTTPException(415, "PDF 또는 XLSX 파일을 선택해 주세요.")
        _check_upload_size(content)
        if extension == ".pdf" and not content.startswith(b"%PDF-"):
            raise HTTPException(422, "올바른 PDF 파일이 아닙니다.")
        if extension == ".xlsx" and not content.startswith(b"PK"):
            raise HTTPException(422, "올바른 Excel 파일이 아닙니다.")
        if extension == ".xlsx":
            from .excel_importer import validate_xlsx_archive
            try:
                validate_xlsx_archive(content)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
        sha = hashlib.sha256(content).hexdigest()
        bank_id = "b_" + sha[:24]
        with self.lock, self.bank_write_context(bank_id):
            try:
                existing = self.store.get("banks", bank_id)
            except KeyError:
                existing = None
            if existing is not None:
                if existing.get('deletion_pending'):
                    raise HTTPException(409, '단어장 완전 삭제를 마친 뒤 다시 등록해 주세요.')
                missing_id = legacy_original_id and legacy_original_id not in existing.get("source", {}).get("legacy_original_ids", [])
                if missing_id or existing.get("deleted_at"):
                    existing.pop("deleted_at", None)
                    if missing_id:
                        existing.setdefault("source", {}).setdefault("legacy_original_ids", []).append(legacy_original_id)
                        self.store.put("banks", existing, expected_version=existing["version"])
                    else:
                        self.store.put_metadata("banks", existing, expected_version=existing["version"])
                return {"status": "existing", "bank_id": existing["id"]}
            for job in self.store.list("jobs"):
                if job["kind"] == "import" and job["payload"]["sha256"] == sha:
                    if legacy_original_id and legacy_original_id not in job["payload"].get("legacy_original_ids", []):
                        coordinator = getattr(self, "import_coordinator", None)
                        if job.get("engine_version") == 2 and coordinator is not None:
                            # 작업 상태를 기록하는 작업자가 링크 목록을 덮어쓰지 않도록 준비 기록에서 병합한다.
                            coordinator.stage_prepare(job["id"], linked_original=legacy_original_id)
                        else:
                            job["payload"].setdefault("legacy_original_ids", []).append(legacy_original_id)
                            job = self.store.put("jobs", job, expected_version=job["version"])
                    if job["status"] in {"failed", "interrupted"}:
                        if job.get('registration_policy') == 'registration-audit-v1' and getattr(self, 'import_coordinator', None):
                            self.import_coordinator.retry(job['id'])
                            return {"status": "processing", "job_id": job['id']}
                        job = self.update_job(job["id"], status="queued", message="등록 재시도", automatic_recovery_version=1,
                            import_in_flight=None, import_recovery={}, recovery={"active": False, "attempt": 0},
                            requires_attention=False, started_at=now())
                        self.dispatch(job)
                    return {"status": "processing", "job_id": job["id"]}
            directory = self.store.root / "uploads"
            directory.mkdir(parents=True, exist_ok=True)
            stored_name = sha + extension
            (directory / stored_name).write_bytes(content)
            payload = {"sha256": sha, "filename": filename, "stored_name": stored_name}
            if legacy_original_id:
                payload["legacy_original_ids"] = [legacy_original_id]
            job = self.store.put("jobs", {"id": "j_" + uuid.uuid4().hex, "kind": "import", "status": "queued", "automatic_recovery_version": 1,
                "payload": payload, "message": "단어은행 등록 대기",
                **self.new_import_fields()})
            self.dispatch(job)
        return {"status": "processing", "job_id": job["id"]}

    def assert_new_import_allowed(self):
        """Only new intake observes the host's admission control; legacy is inert."""
        guard = getattr(self, 'import_admission_guard', None)
        if guard is not None:
            guard()

    def update_job(self, job_id, **changes):
        with self.lock:
            job = self.store.get("jobs", job_id)
            job.update(changes)
            return self.store.put("jobs", job)

    def dismiss_job_message(self, job_id):
        """Remove a message from the UI while preserving work and request identity."""
        with self.lock:
            job = self.get("jobs", job_id)
            if not job.get("dismissed_at") or job.get('preparation_policy') == 'before_review_v1':
                job["dismissed_at"] = now()
                try:
                    job = self.store.put("jobs", job, expected_version=job["version"])
                except ConflictError as error:
                    raise HTTPException(409, str(error)) from None
            return {"job_id": job_id, "dismissed_at": job["dismissed_at"]}

    def dispatch(self, job):
        with self.lock:
            if job["id"] in self.active:
                return job
            self.active.add(job["id"])
            self.executor.submit(self._run, job["id"])
        return job

    def _run(self, job_id):
        job = self.update_job(job_id, status="running", message="처리 중")
        try:
            if job["kind"] == "import":
                self._import(job)
            else:
                self._generate(job)
        except Exception as error:
            from .exams import ExamError
            message = str(error) if isinstance(error, (ValueError, ExamError)) else "처리에 실패했습니다. 저장된 작업에서 재시도할 수 있습니다."
            self.update_job(job_id, status="failed", message=message[:1000], error_code=getattr(error, "code", "processing_failed"))
        finally:
            with self.lock:
                self.active.discard(job_id)

    def _import(self, job):
        if self.import_provider is not None:
            while job.get("status") != "complete":
                try:
                    job = self._import_step(job)
                except Exception as error:
                    recovered = self._recover_import_error(job["id"], error)
                    if recovered is None:
                        raise
                    job = recovered
                if job.get("status") in {"failed", "interrupted"}:
                    return
                # This method runs only in the local background adapter. The
                # Cloud Run adapter executes one durable step per HTTP request.
                delay = self._import_wait_seconds(job.get("recovery", {}).get("next_attempt_at"))
                if delay:
                    time.sleep(min(delay, 30))
            return
        from .excel_importer import import_excel
        from .importer import import_pdf
        payload = job["payload"]
        path = self.ensure_source(job)
        def progress(event):
            self.update_job(job["id"], progress=event)
        if path.suffix == ".pdf":
            bank = import_pdf(path, ocr_engine=self.ocr_engine, progress=progress)
        else:
            bank = import_excel(path, progress=progress)
        self._save_import_bank(job, bank)

    def _import_step(self, job):
        from .ai_importer import (BankImportError, IMPORT_VERSION, IMPORT_LEASE_SECONDS, prepare, extract, enrich,
                                  migrate_pending_chunks, recheck_source, recover_spatial_entries)
        job_id = job["id"]
        if job.get("automatic_recovery_version"):
            recovery = job.get("recovery", {})
            if recovery.get("active") and self._import_wait_seconds(recovery.get("next_attempt_at")) > 0:
                return job
            lease = job.get("import_in_flight")
            if lease:
                if self._import_wait_seconds(lease.get("expires_at")) > 0:
                    return job
                from .providers import ProviderUnavailable
                return self._recover_import_error(job_id, ProviderUnavailable("이전 AI 연결이 종료되어 같은 원문을 다시 확인합니다.", category="connection"))
        phase = job.get("import_phase")
        if not phase:
            if job.get("automatic_recovery_version"):
                lease = {"token": uuid.uuid4().hex, "expires_at": self._import_timestamp(IMPORT_LEASE_SECONDS)} if getattr(self, "work_mode", None) == "request" else None
                job = self.update_job(job_id, status="running", started_at=job.get("started_at") or now(),
                    import_in_flight=lease,
                    message="원문 파일에서 단어와 뜻을 읽고 있습니다.", progress={"stage": "source_read", "completed": 0, "total": 0})
            path = self.ensure_source(job)
            bank, chunks = prepare(path, self.ocr_engine)
            return self.update_job(job_id, status="queued", import_phase="extract" if chunks else "learn",
                import_bank=bank, import_chunks=chunks, import_cursor=0, import_version=IMPORT_VERSION,
                **({"import_source_cells_version": 1} if job.get("automatic_recovery_version") and path.suffix.lower() == ".xlsx" else {}),
                **self._import_success_state(job),
                message="AI 단어장 정리 대기", progress={"stage": "import", "completed": 0, "total": len(chunks) or len(bank["entries"])})
        if job.get("automatic_recovery_version") and job.get("import_source_cells_version") != 1 and Path(job["payload"]["stored_name"]).suffix.lower() == ".xlsx":
            return self._migrate_import_source_cells(job)
        bank, cursor = job["import_bank"], job.get("import_cursor", 0)
        if phase == "extract":
            if job.get("import_version", 1) < IMPORT_VERSION:
                path = self.ensure_source(job)
                if cursor == 0 and not bank.get("entries"):
                    # No model batch has succeeded yet. Recover the original
                    # spatial parser instead of replaying a flattened page.
                    bank, chunks = prepare(path, self.ocr_engine)
                    return self.update_job(job_id, status="queued", import_version=IMPORT_VERSION,
                        import_bank=bank, import_chunks=chunks, import_cursor=0,
                        import_phase="extract" if chunks else "learn", message="원문 구조를 복원했습니다. 등록을 계속합니다.",
                        progress={"stage": "import", "completed": 0, "total": len(chunks) or len(bank["entries"])})
                chunks = migrate_pending_chunks(path, job["import_chunks"], cursor, self.ocr_engine)
                return self.update_job(job_id, status="queued", import_version=IMPORT_VERSION,
                    import_chunks=chunks, message="완료된 항목을 보존하고 남은 원문 구조를 복원했습니다.",
                    progress={"stage": "import", "completed": cursor, "total": len(chunks)})
            chunks = job["import_chunks"]
            blocks = chunks[cursor]
            repair = self._import_repair_context(job, "extract", blocks)
            if repair and job.get("import_recovery", {}).get("attempt", 0) >= 2:
                bank = recover_spatial_entries(bank, blocks)
            else:
                bank = None
            if bank is None:
                self._start_import_model(job, "extract", cursor, IMPORT_LEASE_SECONDS)
                bank = extract(self.import_provider, job["import_bank"], blocks, **({"repair_context": repair} if repair else {}))
            cursor += 1
            if cursor == len(chunks) and not bank["entries"]:
                raise BankImportError("원본에서 단어와 뜻을 찾지 못했습니다.", validation_code="source_empty", retryable=False)
            finished = cursor == len(chunks)
            return self.update_job(job_id, status="queued", import_bank=bank,
                import_cursor=0 if finished else cursor, import_phase="learn" if finished else "extract",
                **self._import_success_state(job),
                message="AI 학습자료 정리 대기" if finished else "AI 원문 정리 중",
                progress={"stage": "import", "completed": cursor, "total": len(chunks)})
        if phase == "learn" and cursor < len(bank["entries"]):
            size = max(1, min(8, job.get("import_learning_batch_size", 8)))
            batch = bank["entries"][cursor:cursor + size]
            repair = self._import_repair_context(job, "learn", batch)
            self._start_import_model(job, "learn", cursor, IMPORT_LEASE_SECONDS)
            bank["entries"][cursor:cursor + size] = enrich(self.import_provider, batch, **({"repair_context": repair} if repair else {}))
            cursor += len(batch)
            return self.update_job(job_id, status="queued", import_bank=bank, import_cursor=cursor,
                **self._import_success_state(job),
                message="단어은행 저장을 준비하고 있습니다." if cursor == len(bank["entries"]) else "AI 단어장 정리 중",
                progress={"stage": "saving" if cursor == len(bank["entries"]) else "learning", "completed": cursor, "total": len(bank["entries"])})
        pending = sum(bool(entry.get("learning_pending")) for entry in bank["entries"])
        if job.get("automatic_recovery_version"):
            self.update_job(job_id, status="running", import_phase="save", learning_pending_count=pending,
                            message="원문 단어와 뜻을 단어은행에 저장하고 있습니다.", progress={"stage": "saving", "completed": 0, "total": 1})
        bank["construction"] = {"mode": "ai", "version": 1, "provider": "gpt", "learning_origin": "ai_enrichment"}
        if pending:
            bank["learning_pending_count"] = pending
            bank["construction"].update(learning_complete=False, learning_pending_count=pending)
        self._save_import_bank(job, bank)
        return self.get("jobs", job_id)

    def _migrate_import_source_cells(self, job):
        """Strengthen explicitly resumed Excel checkpoints without redoing AI.

        Completed entries stay unchanged and must match full reread source
        cells. Only pending extraction blocks receive fresh cell provenance.
        GET/list never invokes this request-driven migration.
        """
        from .ai_importer import BankImportError, _cell_equal, recheck_source
        bank = job["import_bank"]
        chunks = job.get("import_chunks") or []
        cursor = job.get("import_cursor", 0)
        path = self.ensure_source(job)
        if bank.get("entries"):
            recheck_source(path, job["payload"]["sha256"], "learn", bank, bank["entries"],
                           saved_chunks=chunks, ocr_engine=self.ocr_engine)
        elif job.get("import_phase") != "extract":
            raise BankImportError("완료된 단어 항목이 없어 원문 보존을 확인하지 못했습니다.",
                                  validation_code="source_empty", retryable=False)
        changes = {"import_source_cells_version": 1}
        if chunks:
            all_blocks = [block for chunk in chunks for block in chunk]
            context = recheck_source(path, job["payload"]["sha256"], "extract", bank, all_blocks,
                                     saved_chunks=chunks, ocr_engine=self.ocr_engine)
            fresh = {block["id"]: block for block in context["original_source"]}
            complete_chunks = chunks[:cursor] if job.get("import_phase") == "extract" else chunks
            for chunk in complete_chunks:
                for block in chunk:
                    pairs = fresh[block["id"]]["vocabulary_cells"]
                    if pairs is None or any(not any(block["id"] in entry.get("source", {}).get("refs", [])
                        and _cell_equal(pair["word"], entry.get("word_raw", entry["word"]))
                        and _cell_equal(pair["meaning"], entry["meaning_raw"]) for entry in bank["entries"])
                        for pair in (pairs or [])):
                        raise BankImportError("이전 처리 항목이 원문 셀의 모든 단어와 뜻을 보존하는지 확인하지 못했습니다.",
                                              validation_code="source_omitted", retryable=False)
        if chunks and job.get("import_phase") == "extract":
            upgraded = copy.deepcopy(chunks)
            for chunk in upgraded[cursor:]:
                for block in chunk:
                    current = fresh[block["id"]]
                    block.update(cells=current["cells"], vocabulary_cells=current["vocabulary_cells"])
            changes["import_chunks"] = upgraded
        return self.update_job(job["id"], status="queued", import_in_flight=None,
            message="원문 셀 대조를 마쳤습니다. 완료된 항목을 보존하고 등록을 계속합니다.", **changes)

    @staticmethod
    def _import_wait_seconds(value):
        if not value:
            return 0
        try:
            return max(0, datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() - time.time())
        except (ValueError, TypeError, AttributeError):
            return 0

    @staticmethod
    def _import_timestamp(offset=0):
        return datetime.fromtimestamp(time.time() + offset, timezone.utc).isoformat()

    @staticmethod
    def _import_success_state(job):
        if not job.get("automatic_recovery_version"):
            return {}
        previous = job.get("import_recovery", {})
        return {"import_in_flight": None, "import_recovery": {"total_attempts": previous.get("total_attempts", 0)},
                "import_learning_batch_size": 8, "recovery": {"active": False, "attempt": 0,
                    "total_attempts": previous.get("total_attempts", 0)}, "error_code": None, "requires_attention": False}

    def _start_import_model(self, job, phase, cursor, lease_seconds):
        if not job.get("automatic_recovery_version"):
            if phase == "learn":
                return self.update_job(job["id"], status="running", message="AI가 단어장·예문을 정리하고 있습니다.",
                    progress={"stage": "learning", "completed": cursor, "total": len(job["import_bank"]["entries"])})
            return job
        recovery = copy.deepcopy(job.get("recovery", {}))
        if recovery.get("active"):
            recovery.update(stage="retrying", next_attempt_at=None)
        lease = {"token": uuid.uuid4().hex, "expires_at": self._import_timestamp(lease_seconds)} if getattr(self, "work_mode", None) == "request" else None
        return self.update_job(job["id"], status="running", import_in_flight=lease, recovery=recovery,
            message="원문 대조를 마치고 AI가 자동 재시도하고 있습니다." if recovery.get("active") else
                "AI가 원문 단어와 모든 뜻을 정리하고 있습니다." if phase == "extract" else "AI가 단어장·예문을 정리하고 있습니다.",
            progress={"stage": "extract" if phase == "extract" else "learning", "completed": cursor,
                      "total": len(job.get("import_chunks", [])) if phase == "extract" else len(job["import_bank"]["entries"])})

    def _import_repair_context(self, job, phase, items):
        if not job.get("automatic_recovery_version") or not job.get("recovery", {}).get("active"):
            return None
        from .ai_importer import recheck_source, IMPORT_LEASE_SECONDS
        recovery = copy.deepcopy(job["recovery"])
        recovery.update(stage="source_recheck", next_attempt_at=None)
        lease = {"token": uuid.uuid4().hex, "expires_at": self._import_timestamp(IMPORT_LEASE_SECONDS)} if getattr(self, "work_mode", None) == "request" else None
        self.update_job(job["id"], status="running", recovery=recovery, import_in_flight=lease,
            message="원문을 다시 읽어 단어와 모든 뜻을 대조하고 있습니다.",
            progress={"stage": "source_recheck", "completed": job.get("import_cursor", 0),
                      "total": len(job.get("import_chunks", [])) if phase == "extract" else len(job["import_bank"]["entries"])})
        context = recheck_source(self.ensure_source(job), job["payload"]["sha256"], phase, job["import_bank"], items,
                                saved_chunks=job.get("import_chunks"), ocr_engine=self.ocr_engine)
        diagnostic = job.get("import_recovery", {})
        context.update(validation_failure={"code": diagnostic.get("error_code", "invalid_response"),
                                          "details": diagnostic.get("details", {})},
                       attempt=diagnostic.get("attempt", 1),
                       instruction="Correct this rejected batch by quoting only the reread original source. Do not change original vocabulary or previously completed entries.")
        return context

    @staticmethod
    def _import_error_kind(error):
        from .ai_importer import BankImportError
        from .providers import ProviderUnavailable
        if isinstance(error, BankImportError):
            return error.validation_code, error.retryable, False, error.details
        if isinstance(error, ProviderUnavailable):
            permanent = error.category in {"auth", "model_not_found", "unsupported_model", "unsupported_reasoning", "invalid_schema"} or error.status in {400, 401, 403, 404}
            return error.category, not permanent, True, {}
        code = getattr(error, "code", None)
        if code in {"account_mismatch", "source_mismatch", "missing_source", "invalid_source", "ambiguous_record", "ambiguous_destination"}:
            return code, False, False, {}
        # DriveSync preserves partial-save IDs while wrapping transport errors.
        # Inspect only explicit causes and fixed exception/status categories;
        # never expose provider bodies, credentials, or raw diagnostic messages.
        cause, seen = error, set()
        for _ in range(8):
            if cause is None or id(cause) in seen:
                break
            seen.add(id(cause))
            status = getattr(getattr(cause, "resp", None), "status", None)
            if status in {400, 401, 403, 404}:
                return "storage_permission", False, False, {}
            if isinstance(cause, (OSError, TimeoutError, ConflictError)) or status in {408, 409, 429, 500, 502, 503, 504}:
                return "storage_or_connection", True, False, {}
            cause = getattr(cause, "__cause__", None)
        return None

    def _defer_import_learning(self, job, error_code, *, all_remaining=False):
        from .ai_importer import recheck_source
        bank = copy.deepcopy(job["import_bank"])
        cursor = job.get("import_cursor", 0)
        size = len(bank["entries"]) - cursor if all_remaining else min(job.get("import_learning_batch_size", 8), len(bank["entries"]) - cursor)
        entries = bank["entries"][cursor:cursor + size]
        recheck_source(self.ensure_source(job), job["payload"]["sha256"], "learn", bank, entries,
                       saved_chunks=job.get("import_chunks"), ocr_engine=self.ocr_engine)
        for entry in entries:
            entry["learning_pending"] = {"code": error_code, "message": "원문 단어와 뜻은 확인했으며 AI 보충 학습자료 생성을 보류했습니다."}
        cursor += size
        pending = sum(bool(entry.get("learning_pending")) for entry in bank["entries"])
        return self.update_job(job["id"], status="queued", import_bank=bank, import_cursor=cursor,
            import_phase="save" if cursor == len(bank["entries"]) else "learn", learning_pending_count=pending,
            **self._import_success_state(job),
            message="원문을 보존하고 보충 학습자료 생성을 보류한 채 등록을 계속합니다.",
            progress={"stage": "saving" if cursor == len(bank["entries"]) else "learning", "completed": cursor, "total": len(bank["entries"])})

    def _recover_import_error(self, job_id, error):
        from .ai_importer import IMPORT_MAX_ATTEMPTS, IMPORT_RETRY_DELAYS
        job = self.get("jobs", job_id)
        if job.get("kind") != "import" or not job.get("automatic_recovery_version"):
            return None
        if job.get("status") == "complete":
            return job
        kind = self._import_error_kind(error)
        if kind is None and not job.get("import_phase") and isinstance(error, ValueError):
            kind = ("source_unreadable", False, False, {})
        if kind is None:
            return None
        code, retryable, provider_error, details = kind
        phase, cursor = job.get("import_phase", "prepare"), job.get("import_cursor", 0)
        previous = job.get("import_recovery", {})
        attempt = (previous.get("attempt", 0) if previous.get("phase") == phase and previous.get("cursor") == cursor else 0) + 1
        total = previous.get("total_attempts", 0) + 1
        terminal = not retryable or attempt >= IMPORT_MAX_ATTEMPTS
        if terminal and phase == "learn" and (retryable or provider_error):
            try:
                return self._defer_import_learning(dict(job, import_recovery={"total_attempts": total}), code, all_remaining=provider_error)
            except Exception as source_error:
                kind = self._import_error_kind(source_error)
                code = kind[0] if kind else "source_unreadable"
                terminal = True
        if terminal:
            recovery = {"active": False, "attempt": attempt, "total_attempts": total, "max_attempts": IMPORT_MAX_ATTEMPTS,
                        "stage": "attention", "error_code": code, "next_attempt_at": None}
            return self.update_job(job_id, status="failed", import_in_flight=None, recovery=recovery, requires_attention=True,
                error_code=code, message="원문을 보존한 채 자동 복구를 시도했지만 등록을 마치지 못했습니다. "
                    + ("계정·권한 또는 원본 파일을 확인해 주세요." if not retryable else "원문을 확인할 수 있는 파일이나 서비스 연결 상태를 확인해 주세요."))
        changes = {}
        if attempt >= 2 and phase == "learn":
            changes["import_learning_batch_size"] = max(1, (job.get("import_learning_batch_size", 8) + 1) // 2)
        if attempt >= 2 and phase == "extract":
            chunks = copy.deepcopy(job["import_chunks"])
            blocks = chunks[cursor]
            if len(blocks) > 1:
                split = (len(blocks) + 1) // 2
                chunks[cursor:cursor + 1] = [blocks[:split], blocks[split:]]
                changes["import_chunks"] = chunks
        delay = IMPORT_RETRY_DELAYS[min(attempt - 1, len(IMPORT_RETRY_DELAYS) - 1)]
        recovery = {"active": True, "attempt": attempt, "total_attempts": total, "max_attempts": IMPORT_MAX_ATTEMPTS,
                    "stage": "retry_wait", "reason": "원문 대조 후 자동 복구", "error_code": code,
                    "next_attempt_at": self._import_timestamp(delay)}
        diagnostic = {"phase": phase, "cursor": cursor, "attempt": attempt, "total_attempts": total,
                      "error_code": code, "details": details}
        return self.update_job(job_id, status="queued", import_in_flight=None, recovery=recovery,
            import_recovery=diagnostic, error_code=None, requires_attention=False,
            message="원문을 다시 확인한 뒤 자동 재시도합니다.",
            progress={"stage": "retry_wait", "completed": cursor, "total": len(job.get("import_chunks", [])) if phase == "extract" else len(job.get("import_bank", {}).get("entries", []))}, **changes)

    def _save_import_bank(self, job, bank):
        from .source_integrity import freeze_source_manifest, merge_manual_edits
        payload = job["payload"]
        bank["id"] = "b_" + payload["sha256"][:24]
        bank["source"]["filename"] = payload["filename"]
        bank["source"]["stored_name"] = payload["stored_name"]
        bank["source"]["sha256"] = payload["sha256"]
        bank["title"] = Path(payload["filename"]).stem.strip() or "단어장"
        bank["storage"] = "local"
        # 편집·등록 취소와 같은 잠금 순서로 최신 작업의 링크를 보존한다.
        with self.lock, self.bank_write_context(bank["id"]):
            latest_ids = self.store.get("jobs", job["id"])["payload"].get("legacy_original_ids", [])
            if latest_ids:
                bank["source"]["legacy_original_ids"] = list(latest_ids)
            try:
                old = self.store.get("banks", bank["id"])
            except KeyError:
                old = None
            bank = merge_manual_edits(bank, old)
            bank = freeze_source_manifest(bank, evidence="legacy_unverified")
            bank["counts"] = self.counts(bank)
            pending = sum(bool(entry.get("learning_pending") or entry.get("learning_stale")) for entry in bank["entries"])
            if pending or "learning_pending_count" in bank or "learning_complete" in bank.get("construction", {}):
                bank["learning_pending_count"] = pending
                bank.setdefault("construction", {}).update(learning_complete=not pending, learning_pending_count=pending)
            self.store.put("banks", bank, expected_version=old["version"] if old else 0)
            message = "단어은행 등록 완료" + (f" · 학습자료 {pending}개 생성 보류(원문 보존)" if pending else "")
            self.update_job(job["id"], status="complete", result_id=bank["id"], message=message,
                            import_bank=None, import_chunks=None, learning_pending_count=pending, **self._import_success_state(job))

    @staticmethod
    def counts(bank):
        entries = bank["entries"]
        return {"total": len(entries), "page_issues": len(bank.get("page_issues", [])), **{state: sum(entry["status"] == state for entry in entries) for state in ("ready", "review", "excluded")}}

    def _generate(self, job):
        from .exams import select_words, create_worksheet, validate_worksheet
        from .script_generator import generate_script_questions
        from .meaning_writing import generate_writing_questions, create_writing_worksheet
        payload = job["payload"]
        writing = payload.get("worksheet_kind") == "meaning_writing_test"
        if "selected_words" not in job:
            bank = self.get("banks", payload["bank_id"])
            words = select_words(bank, payload["unit_ids"], count=payload["count"], shuffle=True if writing else payload["shuffle"], seed=payload["seed"], allow_partial=payload["allow_partial"])
            job = self.update_job(job["id"], selected_words=words, bank_snapshot=bank, completed_batches={})
        else:
            words, bank = job["selected_words"], job["bank_snapshot"]
        if job.get("generation_mode") != "script":
            job = self.update_job(job["id"], generation_mode="script", completed_batches={}, pending_batch=None, phase_response=None, in_flight=None, payload=self.script_params(payload))
        done = job.get("completed_batches", {})
        for batch_number, offset in enumerate(range(0, len(words), 12)):
            key = str(batch_number)
            if key in done:
                continue
            batch = words[offset:offset + 12]
            self.update_job(job["id"], progress={"completed": offset, "total": len(words), "message": f"{offset + 1}~{min(offset + 12, len(words))}번 스크립트 생성"})
            generator = generate_writing_questions if writing else generate_script_questions
            questions = generator(batch, bank, seed=payload["seed"])
            for question, word in zip(questions, batch):
                question["source_snapshot"] = copy.deepcopy(word)
                question["source_unit_labels"] = [unit["label"] for unit in bank["units"] if unit["id"] in word["unit_ids"]]
            done[key] = questions
            self.update_job(job["id"], completed_batches=done)
        questions = [q for key in sorted(done, key=int) for q in done[key]]
        creator = create_writing_worksheet if writing else create_worksheet
        worksheet = creator(bank, payload["unit_ids"], questions, title=payload.get("title") or bank["title"] + (" 뜻 쓰기 시험" if writing else " 단어–뜻 시험"), selected_words=words, allow_partial=payload["allow_partial"])
        worksheet["id"] = "w_" + job["id"].removeprefix("j_")
        worksheet["bank_id"] = payload["bank_id"]
        worksheet["generation_bank_snapshot"] = copy.deepcopy(bank)
        worksheet["selection"] = {key: payload[key] for key in ("count", "shuffle", "seed", "allow_partial", "unit_ids")}
        worksheet["range_label"] = " · ".join(unit["label"] for unit in bank["units"] if unit["id"] in payload["unit_ids"])
        validate_worksheet(worksheet, require_reviewed=False)
        from .render import render_worksheet
        directory = self.store.root / "pdfs" / worksheet["id"]
        directory.mkdir(parents=True, exist_ok=True)
        result = render_worksheet(worksheet, directory / "student.pdf", directory / "answer.pdf")
        worksheet = self.publish_pdfs(worksheet, result)
        worksheet["pdfs"] = {kind: f"{PREFIX}/worksheets/{worksheet['id']}/pdf/{kind}" for kind in ("student", "answer")}
        worksheet["pdfs_stale"] = False
        self.store.put("worksheets", worksheet)
        self.update_job(job["id"], status="complete", result_id=worksheet["id"], progress={"completed": len(words), "total": len(words)}, message="뜻 쓰기 시험 생성 완료" if writing else "스크립트 시험지 생성 완료 · 검수 필요")


def create_router(service):
    router = APIRouter(prefix=PREFIX, tags=["단어–뜻 시험"])

    @router.get("/status")
    def status():
        return service.status()

    @router.get("/banks")
    def banks():
        result = []
        for stored in service.store.list("banks"):
            if stored.get('deleted_at'):
                continue
            bank = service.public_bank(stored)
            row = {key: bank.get(key) for key in ("id", "title", "source", "units", "counts", "version", "created_at", "updated_at")}
            if stored.get('construction', {}).get('engine_version') == 2:
                row['construction'] = bank['construction']
                if 'registration_status' in bank['construction']:
                    row['registration_status'] = bank['construction']['registration_status']
            result.append(row)
        return result

    @router.post("/imports", status_code=202)
    async def upload(file: UploadFile = File(...)):
        from starlette.concurrency import run_in_threadpool
        await run_in_threadpool(service.assert_new_import_allowed)
        try:
            content = await file.read(MAX_UPLOAD_BYTES + 1)
        finally:
            await file.close()
        _check_upload_size(content)
        return await run_in_threadpool(service.register_upload, file.filename, content)

    @router.get("/banks/{bank_id}")
    def bank(bank_id: str, view: Literal["full", "management"] = "full"):
        value = service.public_bank(service.get("banks", bank_id, allow_pending=view == 'management'))
        if view == "management":
            # Management edits source words/meanings; it does not need every
            # stored definition, example and passage to display the word list.
            for entry in value.get("entries", []):
                learning = entry.pop("learning", None)
                entry["learning_available"] = bool(learning) and not entry.get("learning_stale", False)
        return value

    @router.delete("/banks/{bank_id}")
    def delete_bank(bank_id: str, body: DeleteBankBody):
        return service.delete_bank(bank_id, body.version, body.mode)

    @router.post("/banks/{bank_id}/source-review")
    def source_review(bank_id: str, body: SourceReviewBody):
        from .registration_review import start
        coordinator = getattr(service, 'import_coordinator', None)
        if coordinator is None:
            raise HTTPException(503, '원본 검수 작업자가 준비되지 않았습니다.')
        return start(coordinator, bank_id, body.version, body.mode, body.request_id)

    @router.patch("/banks/{bank_id}")
    def rename_bank(bank_id: str, body: RenameBankBody):
        title = body.title.strip()
        if not title:
            raise HTTPException(422, "단어장 이름을 입력해 주세요.")
        with service.lock, service.bank_write_context(bank_id):
            bank = service.get("banks", bank_id)
            if bank["version"] != body.version:
                raise HTTPException(409, "단어장이 변경되었습니다. 목록을 새로고침해 주세요.")
            if bank.get("title") == title:
                return service.public_bank(bank)
            bank["title"] = title
            try:
                service.assert_bank_write_ownership()
                return service.public_bank(service.store.put("banks", bank, expected_version=body.version))
            except ConflictError as error:
                raise HTTPException(409, str(error)) from None

    def save_bank_edit(bank, version):
        from .source_integrity import refresh_bank_state, assert_source_manifest
        refresh_bank_state(bank)
        assert_source_manifest(bank)
        service.assert_bank_write_ownership()
        return service.public_bank(service.store.put("banks", bank, expected_version=version))

    @router.patch("/banks/{bank_id}/edit")
    def edit_bank(bank_id: str, body: BankEditBody):
        from .source_integrity import freeze_source_manifest, apply_entry_edit, apply_unit_edit
        with service.lock, service.bank_write_context(bank_id):
            original = service.get("banks", bank_id)
            if original["version"] != body.version:
                raise HTTPException(409, "단어장이 변경되었습니다. 최신 내용을 다시 열어 주세요.")
            entries = {item.id: item for item in body.entries}
            units = {item.id: item for item in body.units}
            if len(entries) != len(body.entries) or len(units) != len(body.units):
                raise HTTPException(422, "같은 항목이 중복되었습니다.")
            if not set(entries).issubset({e["id"] for e in original["entries"]}) or not set(units).issubset({u["id"] for u in original["units"]}):
                raise HTTPException(404, "수정할 단어 또는 챕터를 찾을 수 없습니다.")
            if body.title is not None and not body.title.strip():
                raise HTTPException(422, "단어장 이름을 입력해 주세요.")
            if any(not item.label.strip() for item in body.units):
                raise HTTPException(422, "챕터 이름을 입력해 주세요.")
            # Work on a frozen copy and persist once, only after every edit validates.
            bank = freeze_source_manifest(original, evidence="legacy_unverified")
            changed = False
            if body.title is not None and bank.get("title") != body.title.strip():
                bank["title"] = body.title.strip()
                changed = True
            at = now()
            try:
                for unit in bank["units"]:
                    item = units.get(unit["id"])
                    if item is not None:
                        changed = apply_unit_edit(unit, label=item.label.strip(), status=item.status, at=at) or changed
                for entry in bank["entries"]:
                    item = entries.get(entry["id"])
                    if item is not None:
                        changed = apply_entry_edit(entry, word=item.word, meaning=item.meaning, status=item.status,
                            reviewer="사용자", at=at, review_reason=item.review_reason) or changed
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            if not changed:
                return service.public_bank(original)
            return save_bank_edit(bank, body.version)

    @router.patch("/banks/{bank_id}/entries")
    def update_entries(bank_id: str, body: EntriesBody):
        from .source_integrity import freeze_source_manifest, apply_entry_edit
        with service.lock, service.bank_write_context(bank_id):
            if not body.reviewer.strip():
                raise HTTPException(422, "검토자를 입력해 주세요.")
            bank = service.get("banks", bank_id)
            if bank["version"] != body.version:
                raise HTTPException(409, "단어장이 변경되었습니다. 최신 내용을 다시 열어 주세요.")
            updates = {item.id: item for item in body.entries}
            if len(updates) != len(body.entries):
                raise HTTPException(422, "같은 어휘 항목이 중복되었습니다.")
            if not set(updates).issubset({entry["id"] for entry in bank["entries"]}):
                raise HTTPException(404, "어휘 항목을 찾을 수 없습니다.")
            original = bank
            bank = freeze_source_manifest(bank, evidence="legacy_unverified")
            changed = False
            for entry in bank["entries"]:
                item = updates.get(entry["id"])
                if item is None:
                    continue
                try:
                    changed = apply_entry_edit(entry, word=item.word, meaning=item.meaning,
                        status=item.status, reviewer=body.reviewer, at=now(),
                        review_reason=item.review_reason) or changed
                except ValueError as error:
                    raise HTTPException(422, str(error)) from None
            if not changed:
                return service.public_bank(original)
            return save_bank_edit(bank, body.version)

    @router.patch("/banks/{bank_id}/entries/{entry_id}")
    def update_entry(bank_id: str, entry_id: str, body: EntryBody):
        from .source_integrity import freeze_source_manifest, apply_entry_edit
        with service.lock, service.bank_write_context(bank_id):
            bank = service.get("banks", bank_id)
            if bank["version"] != body.version:
                raise HTTPException(409, "단어장이 변경되었습니다. 최신 내용을 다시 열어 주세요.")
            original = bank
            bank = freeze_source_manifest(bank, evidence="legacy_unverified")
            entry = next((e for e in bank["entries"] if e["id"] == entry_id), None)
            if not entry:
                raise HTTPException(404, "어휘 항목을 찾을 수 없습니다.")
            try:
                changed = apply_entry_edit(entry, word=body.word, meaning=body.meaning,
                    status=body.status, at=now(), review_reason=body.review_reason)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            if not changed:
                return service.public_bank(original)
            return save_bank_edit(bank, body.version)

    @router.get("/banks/{bank_id}/source")
    def bank_source(bank_id: str):
        bank = service.get("banks", bank_id)
        return FileResponse(service.ensure_source(bank), filename=bank["source"]["filename"])

    @router.patch("/banks/{bank_id}/units/{unit_id}")
    def update_unit(bank_id: str, unit_id: str, body: UnitBody):
        from .source_integrity import freeze_source_manifest, apply_unit_edit
        with service.lock, service.bank_write_context(bank_id):
            bank = service.get("banks", bank_id)
            if bank["version"] != body.version:
                raise HTTPException(409, "단어장이 변경되었습니다. 최신 내용을 다시 열어 주세요.")
            original = bank
            bank = freeze_source_manifest(bank, evidence="legacy_unverified")
            unit = next((u for u in bank["units"] if u["id"] == unit_id), None)
            if unit is None:
                raise HTTPException(404, "단원을 찾을 수 없습니다.")
            label = body.label.strip()
            if not label:
                raise HTTPException(422, "단원명을 입력해 주세요.")
            if not apply_unit_edit(unit, label=label, at=now(), status=body.status, review_reason=body.review_reason):
                return service.public_bank(original)
            return save_bank_edit(bank, body.version)

    @router.get("/banks/{bank_id}/entries/{entry_id}/image")
    def source_crop(bank_id: str, entry_id: str):
        bank = service.get("banks", bank_id)
        entry = next((e for e in bank["entries"] if e["id"] == entry_id), None)
        if not entry or bank["source"]["format"] != "pdf":
            raise HTTPException(404, "원본 부분 이미지를 찾을 수 없습니다.")
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(service.ensure_source(bank))
        try:
            page = doc[entry["source"]["page"] - 1]
            try:
                bitmap = page.render(scale=2.5)
                try:
                    image = bitmap.to_pil()
                    bbox = entry["source"].get("bbox")
                    if bbox and len(bbox) == 4:
                        x0, y0, x1, y1 = bbox
                        image = image.crop((max(0, (x0 - 4) * 2.5), max(0, (y0 - 4) * 2.5), min(image.width, (x1 + 4) * 2.5), min(image.height, (y1 + 4) * 2.5)))
                    output = io.BytesIO()
                    image.save(output, format="PNG")
                    return Response(output.getvalue(), media_type="image/png")
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            doc.close()

    @router.get("/banks/{bank_id}/pages/{page_number}")
    def source_page(bank_id: str, page_number: int):
        bank = service.get("banks", bank_id)
        if bank["source"]["format"] != "pdf" or not 1 <= page_number <= bank["source"]["page_count"]:
            raise HTTPException(404, "원본 페이지를 찾을 수 없습니다.")
        import pypdfium2 as pdfium
        doc = pdfium.PdfDocument(service.ensure_source(bank))
        try:
            page = doc[page_number - 1]
            try:
                bitmap = page.render(scale=1.6)
                try:
                    output = io.BytesIO()
                    bitmap.to_pil().save(output, format="PNG")
                    return Response(output.getvalue(), media_type="image/png")
                finally:
                    bitmap.close()
            finally:
                page.close()
        finally:
            doc.close()

    @router.post("/selection")
    def selection(body: GenerateBody):
        from .exams import ExamError, select_words
        bank = service.get("banks", body.bank_id)
        try:
            if bank.get("page_issues") and not body.allow_partial:
                raise ExamError("확인이 필요한 페이지가 있습니다. 원본 확인 또는 등록된 항목만 출제를 선택해 주세요.", "page_review_required", page_issues=bank["page_issues"])
            words = select_words(bank, body.unit_ids, count=body.count, shuffle=True if body.worksheet_kind == "meaning_writing_test" else body.shuffle, seed=body.seed, allow_partial=body.allow_partial)
            return {"count": len(words), "words": words}
        except ExamError as error:
            raise HTTPException(422, {"message": str(error), "code": error.code, "details": error.details}) from None

    @router.post("/generate", status_code=202)
    def generate(body: GenerateBody):
        params = service.script_params(body.model_dump(exclude={"request_id"}))
        with service.lock:
            existing, job_id = service.find_intent("generate", body.request_id, params)
            if existing:
                return {"job_id": existing["id"]}
            payload = copy.deepcopy(params)
            if payload["title"] is not None and not payload["title"].strip():
                raise HTTPException(422, "시험지 제목을 입력해 주세요.")
            if payload["seed"] is None:
                payload["seed"] = int(uuid.uuid4().hex[:12], 16)
            chosen = selection(body.model_copy(update={"seed": payload["seed"]}))
            bank = service.get("banks", body.bank_id)
            job = service.store.put("jobs", {"id": job_id, "kind": "generate", "generation_mode": "script", "status": "queued", "request_id": body.request_id, "request_payload": params, "payload": payload, "selected_words": chosen["words"], "bank_snapshot": bank, "completed_batches": {}, "message": "스크립트 시험지 생성 대기"})
            service.dispatch(job)
        return {"job_id": job["id"]}

    def bulk_plan(body, bank, seed):
        """Validate every range before queuing any work, using one source snapshot."""
        from .exams import ExamError, select_words
        labels = {unit["id"]: unit["label"] for unit in bank.get("units", [])}
        plans, seen = [], set()
        for index, unit_ids in enumerate(body.ranges):
            label = " · ".join(labels[uid] for uid in labels if uid in unit_ids)
            try:
                if body.count is not None and body.count < 1:
                    raise ExamError("문항 수는 1 이상의 정수여야 합니다.", "invalid_selection")
                if bank.get("page_issues") and not body.allow_partial:
                    raise ExamError("확인이 필요한 페이지가 있습니다. 원본 확인 또는 등록된 항목만 출제를 선택해 주세요.", "page_review_required", page_issues=bank["page_issues"])
                signature = tuple(sorted(unit_ids))
                if signature in seen:
                    raise ExamError("같은 출제 범위가 중복되었습니다.", "invalid_selection")
                available = len(select_words(bank, unit_ids, allow_partial=body.allow_partial))
                if not available:
                    raise ExamError("선택 범위에 출제 가능한 단어가 없습니다.", "empty_selection")
                count = min(body.count, available) if body.count is not None else available
                words = select_words(bank, unit_ids, count=count if body.count is not None else None,
                                     shuffle=True if body.worksheet_kind == "meaning_writing_test" else body.shuffle, seed=seed, allow_partial=body.allow_partial)
                seen.add(signature)
                plans.append({"unit_ids": list(unit_ids), "label": label, "available": available, "count": count, "words": words})
            except ExamError as error:
                details = {**error.details, "range_index": index, "unit_ids": unit_ids, "label": label}
                raise HTTPException(422, {"message": f"{label or str(index + 1) + '번째 범위'}: {error}", "code": error.code, "details": details}) from None
        return plans

    def bulk_response(plans, batch_id=None):
        ranges = [{key: value[key] for key in ("unit_ids", "label", "available", "count", "job_id") if key in value} for value in plans]
        result = {"ranges": ranges, "worksheet_count": len(ranges), "total_count": sum(value["count"] for value in ranges)}
        if batch_id is not None:
            result.update(batch_id=batch_id, job_ids=[value["job_id"] for value in ranges])
        return result

    @router.post("/selection-bulk")
    def selection_bulk(body: BulkGenerateBody):
        bank = service.get("banks", body.bank_id)
        return bulk_response(bulk_plan(body, bank, body.seed))

    @router.post("/generate-bulk", status_code=202)
    def generate_bulk(body: BulkGenerateBody):
        params = service.script_params(body.model_dump(exclude={"request_id"}))
        identity = body.request_id or uuid.uuid4().hex
        batch_id = "bg_" + hashlib.sha256(identity.encode()).hexdigest()[:32]
        job_ids = ["j_" + hashlib.sha256(f"generate-bulk:{identity}:{index}".encode()).hexdigest()[:32]
                   for index in range(len(body.ranges))]

        def check_existing(job):
            previous = job.get("bulk_request_payload")
            if job.get("kind") != "generate" or previous is None or service.script_params(previous) != params:
                raise HTTPException(409, "같은 작업 번호에 다른 입력을 사용할 수 없습니다.")
            return job

        with service.lock:
            try:
                existing = check_existing(service.store.get("jobs", job_ids[0]))
            except KeyError:
                existing = None
            if params["title"] is not None and not params["title"].strip():
                raise HTTPException(422, "시험지 제목을 입력해 주세요.")
            # The first durable child carries the complete intent and source. A
            # lost response or partially persisted batch reuses that snapshot.
            bank = existing["bank_snapshot"] if existing else service.get("banks", body.bank_id)
            seed = existing["bulk_seed"] if existing else body.seed
            if seed is None:
                seed = int(uuid.uuid4().hex[:12], 16)
            plans = bulk_plan(body, bank, seed)
            jobs = []
            for index, plan in enumerate(plans):
                try:
                    job = check_existing(service.store.get("jobs", job_ids[index]))
                except KeyError:
                    # Reusing an old bulk intent must not create new children
                    # from a bank that has since been unregistered.
                    service.get("banks", body.bank_id)
                    payload = copy.deepcopy(params)
                    payload.pop("ranges")
                    payload.update(unit_ids=plan["unit_ids"], count=plan["count"] if body.count is not None else None, seed=seed)
                    suffix = " · " + plan["label"][:100]
                    title = params["title"].strip() if params["title"] else bank["title"] + (" 뜻 쓰기 시험" if params["worksheet_kind"] == "meaning_writing_test" else " 단어–뜻 시험")
                    payload["title"] = title[:160 - len(suffix)] + suffix
                    record = {"id": job_ids[index], "kind": "generate", "generation_mode": "script", "status": "queued",
                              "request_id": "bulk-" + hashlib.sha256(f"{identity}:{index}".encode()).hexdigest()[:32],
                              "request_payload": copy.deepcopy(payload), "payload": payload,
                              "selected_words": plan["words"], "bank_snapshot": bank, "completed_batches": {},
                              "bulk_id": batch_id, "bulk_index": index, "bulk_label": plan["label"],
                              "bulk_available": plan["available"], "bulk_count": plan["count"], "bulk_seed": seed,
                              "bulk_request_id": body.request_id, "bulk_request_payload": params,
                              "message": plan["label"] + " · 시험지 생성 대기"}
                    try:
                        job = service.store.put("jobs", record, expected_version=0)
                    except ConflictError:
                        # Another request may have persisted the same intent.
                        # Re-enter from its first child so every source/seed is
                        # taken from the durable winner rather than this read.
                        return generate_bulk(body.model_copy(update={"request_id": identity}))
                jobs.append(job)
                plan.update(job_id=job["id"], available=job["bulk_available"], count=job["bulk_count"], label=job["bulk_label"])
            for job in jobs:
                if job["status"] == "queued":
                    service.dispatch(job)
        return bulk_response(plans, batch_id)

    @router.get("/jobs")
    def jobs():
        def summarize(job):
            return {**{key: job.get(key) for key in ("id", "kind", "status", "request_id", "message", "progress", "result_id", "created_at") + (("dismissed_at",) if job.get("dismissed_at") else ()) + (("bulk_id", "bulk_index", "bulk_label") if job.get("bulk_id") else ())},
                 "worksheet_kind": job.get("payload", {}).get("worksheet_kind", "word_meaning_test"),
                 **import_job_state(job)}
        rows = public_rows("jobs", summarize)
        # Editing a bank does not change a completed job's cached Drive summary.
        # Project the current retry state after that cache boundary.
        for index, row in enumerate(rows):
            if (row.get('kind') == 'import' and row.get('preparation_policy') == 'before_review_v1'
                    and row.get('status') == 'complete'):
                try:
                    rows[index] = {**row, **job_response(service.read_job(row['id']))}
                except HTTPException as error:
                    if error.status_code != 404:
                        raise
        return rows

    def public_rows(kind, summarize):
        if hasattr(service.store, "list_summaries"):
            return service.store.list_summaries(kind, summarize)
        return [row for value in service.store.list(kind) if (row := summarize(value)) is not None]

    def import_job_state(value):
        if value.get("kind") != "import":
            return {}
        extra = service.public_import_state(value) if hasattr(service, 'public_import_state') else {}
        if not value.get("automatic_recovery_version"):
            return extra
        recovery = value.get("recovery", {})
        fields = ("active", "attempt", "total_attempts", "max_attempts", "stage", "next_attempt_at", "reason", "error_code")
        return {**extra, "automatic_recovery_version": value["automatic_recovery_version"], "created_at": value.get("created_at"),
                "updated_at": value.get("updated_at"), "started_at": value.get("started_at") or value.get("created_at"),
                "requires_attention": bool(value.get("requires_attention")), "learning_pending_count": value.get("learning_pending_count", 0),
                "recovery": {key: recovery[key] for key in fields if key in recovery}}

    def job_response(value):
        return {**{key: value.get(key) for key in ("id", "kind", "status", "request_id", "message", "progress", "result_id", "error_code") + (("dismissed_at",) if value.get("dismissed_at") else ()) + (("bulk_id", "bulk_index", "bulk_label") if value.get("bulk_id") else ())}, "worksheet_kind": value.get("payload", {}).get("worksheet_kind", "word_meaning_test"), **import_job_state(value)}

    @router.get("/jobs/{job_id}")
    def job(job_id: str):
        return job_response(service.read_job(job_id) if hasattr(service, 'read_job') else service.get('jobs', job_id))

    @router.delete("/jobs/{job_id}/message")
    def dismiss_job_message(job_id: str):
        return service.dismiss_job_message(job_id)

    @router.post("/jobs/{job_id}/retry", status_code=202)
    def retry(job_id: str):
        value = service.read_job(job_id) if hasattr(service, 'read_job') else service.get('jobs', job_id)
        target_id = value.get('payload', {}).get('worksheet_id') or value.get('result_id')
        if value.get('kind') == 'generate' and not target_id:
            target_id = 'w_' + job_id.removeprefix('j_')
        if target_id and value.get('kind') != 'import':
            try:
                target = service.store.get('worksheets', target_id)
            except KeyError:
                target = None
            if target and (target.get('deleted_at') or target.get('deletion_pending')):
                raise HTTPException(409, '삭제한 시험지의 작업은 다시 실행할 수 없습니다.')
        if value["status"] not in {"failed", "interrupted"}:
            raise HTTPException(409, "중단되거나 실패한 작업만 재시도할 수 있습니다.")
        if hasattr(service, "retry_job"):
            service.retry_job(job_id)
            return {"job_id": job_id}
        value = service.update_job(job_id, status="queued", message="재시도 대기")
        service.dispatch(value)
        return {"job_id": job_id}

    @router.post("/jobs/{job_id}/step")
    def step(job_id: str):
        if not hasattr(service, "run_step"):
            return job(job_id)
        value = service.run_step(job_id)
        if isinstance(value, dict) and value.get("id") == job_id and value.get("kind") in {"generate", "regenerate", "import"}:
            return job_response(value)
        return job(job_id)

    @router.get("/worksheets")
    def worksheets():
        def summarize(worksheet):
            if worksheet.get('deleted_at'):
                return None
            return {key: worksheet.get(key) for key in ("id", "title", "range_label", "version", "created_at", "updated_at", "pdfs", "pdfs_stale", "drive", "deletion_pending")} | {"worksheet_kind": worksheet.get("worksheet_kind", "word_meaning_test"), "count": len(worksheet["questions"])}
        return public_rows("worksheets", summarize)

    @router.delete('/worksheets/{worksheet_id}')
    def delete_worksheet(worksheet_id: str, body: DeleteWorksheetBody):
        from .deletion import delete_worksheet
        return delete_worksheet(service, worksheet_id, body.mode, body.version)

    @router.get("/worksheets/{worksheet_id}")
    def worksheet(worksheet_id: str):
        return service.get("worksheets", worksheet_id)

    @router.patch("/worksheets/{worksheet_id}")
    def save_worksheet(worksheet_id: str, body: WorksheetBody):
        from .exams import edit_question, validate_worksheet, ExamError
        with service.lock:
            worksheet = service.get("worksheets", worksheet_id)
            if body.title is not None:
                worksheet["title"] = body.title.strip()
            if body.questions is not None:
                if len(body.questions) != len(worksheet["questions"]):
                    raise HTTPException(422, "문항 개수는 편집에서 바꿀 수 없습니다.")
                if any(not isinstance(q.get("id"), str) or not q["id"] for q in body.questions):
                    raise HTTPException(422, "문항 ID가 올바르지 않습니다.")
                updates = {question["id"]: question for question in body.questions}
                if len(updates) != len(body.questions) or set(updates) != {q["id"] for q in worksheet["questions"]}:
                    raise HTTPException(422, "문항 ID가 일치하지 않습니다.")
                edited = []
                try:
                    for question in worksheet["questions"]:
                        new = updates[question["id"]]
                        if worksheet.get("worksheet_kind") == "meaning_writing_test":
                            from .meaning_writing import edit_writing_question
                            edited.append(edit_writing_question(question, word=new.get("word"), answer_text=new.get("answer_text")))
                            continue
                        if any(new.get(key) != question.get(key) for key in ("word", "choices", "correct_choice_ids")):
                            groups = new.get("semantic_groups")
                            if (groups is None or groups == question["semantic_groups"]) and isinstance(new.get("choices"), list) and isinstance(new.get("correct_choice_ids"), list):
                                # Rebind existing exact-source groups when an editor moves
                                # a correct meaning to another choice ID. Never invent a source span.
                                selected_choices = [c for c in new["choices"] if isinstance(c, dict) and c.get("id") in new["correct_choice_ids"]]
                                if len(selected_choices) == len(question["semantic_groups"]):
                                    unmatched = list(selected_choices)
                                    groups = copy.deepcopy(question["semantic_groups"])
                                    for group in groups:
                                        match = next((c for c in unmatched if c.get("text") == group["text"]), None)
                                        if match is None:
                                            groups = None
                                            break
                                        group["choice_id"] = match["id"]
                                        unmatched.remove(match)
                            question = edit_question(question, word=new.get("word"), choices=new.get("choices"), correct_choice_ids=new.get("correct_choice_ids"), semantic_groups=groups)
                        edited.append(question)
                except (ExamError, TypeError, KeyError) as error:
                    raise HTTPException(422, str(error) if isinstance(error, ExamError) else "문항 수정 형식이 올바르지 않습니다.") from None
                worksheet["questions"] = edited
            try:
                validate_worksheet(worksheet, require_reviewed=False)
                worksheet["pdfs_stale"] = True
                return service.store.put("worksheets", worksheet, expected_version=body.version)
            except ConflictError as error:
                raise HTTPException(409, str(error)) from None
            except ExamError as error:
                raise HTTPException(422, str(error)) from None

    @router.post("/worksheets/{worksheet_id}/questions/{question_id}/replace")
    def replace_question(worksheet_id: str, question_id: str, body: ReplaceQuestionBody):
        from .meaning_writing import replace_writing_question
        from .exams import ExamError
        with service.lock:
            worksheet = service.get("worksheets", worksheet_id)
            if worksheet.get("worksheet_kind") != "meaning_writing_test":
                raise HTTPException(422, "뜻 쓰기 시험에서만 다른 단어로 교체할 수 있습니다.")
            receipts = worksheet.get("applied_replacement_requests", {})
            if body.request_id and body.request_id in receipts:
                previous = receipts[body.request_id]
                if previous["question_id"] != question_id or previous["version"] != body.version:
                    raise HTTPException(409, "같은 작업 번호에 다른 입력을 사용할 수 없습니다.")
                return worksheet
            if body.version != worksheet["version"]:
                raise HTTPException(409, "최신 내용을 다시 열어 주세요.")
            if not any(q["id"] == question_id for q in worksheet["questions"]):
                raise HTTPException(404, "문항을 찾을 수 없습니다.")
            try:
                updated = replace_writing_question(worksheet, question_id, bank=service.worksheet_bank(worksheet), seed=int(uuid.uuid4().hex[:12], 16))
                updated["pdfs_stale"] = True
                if body.request_id:
                    updated.setdefault("applied_replacement_requests", {})[body.request_id] = {"question_id": question_id, "version": body.version}
                return service.store.put("worksheets", updated, expected_version=body.version)
            except ConflictError as error:
                raise HTTPException(409, str(error)) from None
            except ExamError as error:
                raise HTTPException(422, {"message": str(error), "code": error.code, "details": error.details}) from None

    @router.post("/worksheets/{worksheet_id}/questions/{question_id}/regenerate")
    def regenerate(worksheet_id: str, question_id: str, body: RegenerateBody):
        from .exams import add_candidate, select_candidate, ExamError
        from .script_generator import generate_script_questions
        if service.get("worksheets", worksheet_id).get("worksheet_kind") == "meaning_writing_test":
            raise HTTPException(422, "뜻 쓰기 시험에서는 다른 단어로 교체 기능을 사용하세요.")
        if hasattr(service, "queue_regeneration"):
            return service.queue_regeneration(worksheet_id, question_id, body.version, {}, body.request_id)
        worksheet = service.get("worksheets", worksheet_id)
        if body.version != worksheet["version"]:
            raise HTTPException(409, "최신 내용을 다시 열어 주세요.")
        question = next((q for q in worksheet["questions"] if q["id"] == question_id), None)
        if question is None:
            raise HTTPException(404, "문항을 찾을 수 없습니다.")
        if len(question.get("candidates", [])) >= 10:
            raise HTTPException(422, "더 이상 생성할 수 없습니다.")
        word = copy.deepcopy(question.get("source_snapshot"))
        if not word:
            raise HTTPException(422, "재생성에 필요한 원본 스냅샷이 없습니다.")
        word["source_word"] = question.get("source_word", word["word"])
        word["word"] = question["word"]
        try:
            bank = service.worksheet_bank(worksheet)
            generated = generate_script_questions([word], bank, seed=int(uuid.uuid4().hex[:12], 16))
            candidate = generated[0]
            candidate["source_snapshot"] = copy.deepcopy(question["source_snapshot"])
            candidate["source_unit_labels"] = question.get("source_unit_labels", [])
            if "source_word" not in question:
                candidate.pop("source_word", None)
            updated_question = add_candidate(question, candidate)
            updated_question = select_candidate(updated_question, updated_question["candidates"][-1]["id"])
            with service.lock:
                worksheet["questions"] = [updated_question if q["id"] == question_id else q for q in worksheet["questions"]]
                worksheet["pdfs_stale"] = True
                worksheet.setdefault("generation_bank_snapshot", copy.deepcopy(bank))
                return service.store.put("worksheets", worksheet, expected_version=body.version)
        except ConflictError as error:
            raise HTTPException(409, str(error)) from None
        except (ExamError, ValueError) as error:
            raise HTTPException(422, str(error)) from None

    @router.post("/worksheets/{worksheet_id}/questions/{question_id}/candidate")
    def choose_candidate(worksheet_id: str, question_id: str, body: CandidateBody):
        from .exams import select_candidate, ExamError
        with service.lock:
            worksheet = service.get("worksheets", worksheet_id)
            if worksheet.get("worksheet_kind") == "meaning_writing_test":
                raise HTTPException(422, "뜻 쓰기 시험에는 객관식 후보가 없습니다.")
            question = next((q for q in worksheet["questions"] if q["id"] == question_id), None)
            if question is None:
                raise HTTPException(404, "문항을 찾을 수 없습니다.")
            try:
                selected = select_candidate(question, body.candidate_id)
                worksheet["questions"] = [selected if q["id"] == question_id else q for q in worksheet["questions"]]
                worksheet["pdfs_stale"] = True
                return service.store.put("worksheets", worksheet, expected_version=body.version)
            except ConflictError as error:
                raise HTTPException(409, str(error)) from None
            except ExamError as error:
                raise HTTPException(422, str(error)) from None

    @router.post("/worksheets/{worksheet_id}/review")
    def review(worksheet_id: str, body: ReviewBody):
        from .exams import confirm_question_review
        with service.lock:
            worksheet = service.get("worksheets", worksheet_id)
            if not set(body.question_ids).issubset({q["id"] for q in worksheet["questions"]}):
                raise HTTPException(422, "문항 ID가 일치하지 않습니다.")
            worksheet["questions"] = [confirm_question_review(question, reviewer="사용자") if question["id"] in body.question_ids else question for question in worksheet["questions"]]
            worksheet["pdfs_stale"] = True
            try:
                return service.store.put("worksheets", worksheet, expected_version=body.version)
            except ConflictError as error:
                raise HTTPException(409, str(error)) from None

    @router.post("/worksheets/{worksheet_id}/render")
    def render(worksheet_id: str):
        from .exams import validate_worksheet, ExamError
        from .render import render_worksheet
        with service.lock:
            worksheet = service.get("worksheets", worksheet_id)
            try:
                validate_worksheet(worksheet, require_reviewed=False)
                directory = service.store.root / "pdfs" / worksheet_id
                directory.mkdir(parents=True, exist_ok=True)
                result = render_worksheet(worksheet, directory / "student.pdf", directory / "answer.pdf")
            except (ExamError, ValueError) as error:
                raise HTTPException(422, str(error)) from None
            worksheet["pdfs"] = {"student": f"{PREFIX}/worksheets/{worksheet_id}/pdf/student", "answer": f"{PREFIX}/worksheets/{worksheet_id}/pdf/answer"}
            worksheet["pdfs_stale"] = False
            worksheet = service.publish_pdfs(worksheet, result)
            saved = service.store.put("worksheets", worksheet, expected_version=worksheet["version"])
            return {"worksheet": saved, "render": result}

    @router.get("/worksheets/{worksheet_id}/pdf/{kind}")
    def download(worksheet_id: str, kind: str, download: bool = False):
        with service.lock:
            worksheet = service.get("worksheets", worksheet_id)
            if kind not in {"student", "answer", "combined"} or not worksheet.get("pdfs"):
                raise HTTPException(404, "PDF를 먼저 생성해 주세요.")
            if worksheet.get("pdfs_stale"):
                raise HTTPException(409, "수정한 내용을 반영하려면 PDF를 재생성해 주세요.")
            if kind != "combined":
                label = "문제지" if kind == "student" else "정답지"
                return FileResponse(service.ensure_pdf(worksheet, kind), media_type="application/pdf", filename=f"{worksheet['title']} {label}.pdf", content_disposition_type="attachment" if download else "inline")

            # Read both saved PDFs through the same storage/hash checks as an
            # individual download. The merged response is never saved/uploaded.
            sources = []
            for part in ("student", "answer"):
                if not worksheet["pdfs"].get(part):
                    raise HTTPException(404, "문제지와 정답지 PDF를 먼저 생성해 주세요.")
                path = service.ensure_pdf(worksheet, part)
                try:
                    sources.append(path.read_bytes())
                except FileNotFoundError:
                    raise HTTPException(404, "문제지와 정답지 PDF를 먼저 생성해 주세요.") from None

            from pypdf import PdfReader, PdfWriter
            from pypdf.errors import PdfReadError

            output = io.BytesIO()
            try:
                with PdfWriter() as writer:
                    for source in sources:
                        reader = PdfReader(io.BytesIO(source))
                        if not reader.pages:
                            raise ValueError("empty PDF")
                        writer.append(reader, import_outline=False)
                    writer.write(output)
            except (PdfReadError, ValueError):
                raise HTTPException(422, "저장된 PDF를 읽지 못했습니다. PDF를 재생성해 주세요.") from None
            filename = quote(f"{worksheet['title']} 전체.pdf", safe="")
            disposition = "attachment" if download else "inline"
            return Response(output.getvalue(), media_type="application/pdf", headers={
                "Content-Disposition": f"{disposition}; filename=\"worksheet-combined.pdf\"; filename*=utf-8''{filename}",
                "Cache-Control": "private, no-store",
            })

    return router


def create_app(root=None, provider=None, ocr_engine=None):
    package_parent = Path(__file__).resolve().parent.parent
    static_directory = package_parent / "static"
    if not (static_directory / "styles.css").is_file():
        raise RuntimeError(f"공통 정적 파일 폴더를 찾을 수 없습니다: {static_directory}")
    root = root or Path(__file__).resolve().parent.parent / ".local" / "word-meaning"
    service = FeatureService(root, provider=provider, ocr_engine=ocr_engine)
    app = FastAPI(title="멜로디잉글리시 단어–뜻 시험", docs_url="/api-docs")
    app.state.word_meaning = service
    app.include_router(create_router(service))
    app.mount("/static", StaticFiles(directory=static_directory), name="static")
    @app.get("/", response_class=HTMLResponse)
    def index():
        return (Path(__file__).parent / "static" / "index.html").read_text(encoding="utf-8")
    return app
