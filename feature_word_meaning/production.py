"""Operating-app adapters: request-driven work and durable Drive checkpoints.

One step runs one script batch. No job depends on background CPU after a
Cloud Run HTTP response. Existing AI checkpoints transition to script work.
"""

from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager, nullcontext
from collections import OrderedDict
import hashlib
import importlib
import io
import json
from pathlib import Path
import sys
import threading
import uuid

from fastapi import HTTPException

from .api import FeatureService, PREFIX
from .drive_sync import DriveSync, DriveSyncError, EXPECTED_ACCOUNT, FEATURE_TAG, FOLDER_MIME, FILE_FIELDS, _file_hash, _hash
from .exams import add_candidate, create_worksheet, select_candidate, select_words, validate_worksheet
from .script_generator import generate_script_questions
from .storage import ConflictError, DriveJsonStore


_RECORD_CACHE_LIMIT = 16 * 1024 * 1024
_SUMMARY_CACHE_LIMIT = 2 * 1024 * 1024


def _decoded_record_size(value):
    """Count the retained Python object graph once, including shared keys."""
    seen = set()

    def size(item):
        identity = id(item)
        if identity in seen:
            return 0
        seen.add(identity)
        total = sys.getsizeof(item)
        if isinstance(item, dict):
            total += sum(size(key) + size(child) for key, child in item.items())
        elif isinstance(item, (list, tuple)):
            total += sum(size(child) for child in item)
        return total

    return size(value)


class OperatingDriveBackend:
    """Only the current project's isolated feature folders are enumerated."""
    def __init__(self):
        self._folders = None
        self._folder_lock = threading.RLock()
        self._thread = threading.local()
        self._record_cache = OrderedDict()
        self._record_cache_bytes = 0
        self._record_cache_lock = threading.RLock()
        self._summary_cache = OrderedDict()
        self._summary_cache_bytes = 0

    @contextmanager
    def request_scope(self):
        """Reuse verified clients only during one synchronous generation step."""
        if getattr(self._thread, "request_scope", None) is not None:
            yield
            return
        self._thread.request_scope = {}
        try:
            yield
        finally:
            del self._thread.request_scope

    def _clients(self):
        scope = getattr(self._thread, "request_scope", None)
        if scope is not None and "clients" in scope:
            return scope["clients"]
        clients = importlib.import_module("app.google_clients")
        drive = clients.drive_service()
        email = drive.about().get(fields="user(emailAddress)").execute().get("user", {}).get("emailAddress", "")
        if email.strip().lower() != EXPECTED_ACCOUNT:
            raise DriveSyncError("승인된 GCP·Drive 계정과 연결된 계정이 달라 저장하지 않았습니다.", "account_mismatch")
        sheets = getattr(self._thread, "sheets", None)
        if sheets is None:
            from googleapiclient.discovery import build
            # Both API clients use the operating app's exact same credentials.
            sheets = build("sheets", "v4", credentials=clients._credentials(), cache_discovery=False)
            self._thread.sheets = sheets
        if scope is not None:
            scope["clients"] = (drive, sheets)
        return drive, sheets

    @staticmethod
    def _folder(drive, name, parent=None):
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name='{escaped}' and mimeType='{FOLDER_MIME}' and trashed=false"
        if parent:
            query += f" and '{parent}' in parents"
        files = drive.files().list(q=query, fields="files(id,name),nextPageToken", pageSize=100).execute().get("files", [])
        if len(files) > 1:
            raise DriveSyncError("같은 이름의 프로젝트 폴더가 둘 이상입니다. 대상 폴더를 확인하세요.", "ambiguous_destination")
        if files:
            return files[0]["id"]
        body = {"name": name, "mimeType": FOLDER_MIME}
        if parent:
            body["parents"] = [parent]
        return drive.files().create(body=body, fields="id").execute()["id"]

    def _context(self):
        drive, sheets = self._clients()
        with self._folder_lock:
            if self._folders is None:
                config = importlib.import_module("app.config")
                project = self._folder(drive, config.PROJECT_FOLDER)
                feature = self._folder(drive, "단어–뜻 시험", project)
                self._folders = {
                    "project": project, "feature": feature,
                    "vocab": self._folder(drive, "단어은행", feature),
                    "exam": self._folder(drive, "시험지", feature),
                    "states": self._folder(drive, "상태 기록", feature),
                }
        return drive, sheets, self._folders

    def _sync(self):
        drive, sheets, folders = self._context()
        return DriveSync(drive, sheets, folders["vocab"], folders["exam"])

    def _find(self, kind, record_id=None):
        drive, _, folders = self._context()
        query = f"'{folders['states']}' in parents and trashed=false and appProperties has {{ key='feature' and value='{FEATURE_TAG}' }} and appProperties has {{ key='kind' and value='state_{kind}' }}"
        if record_id:
            query += f" and appProperties has {{ key='record_key' and value='{_hash(record_id)}' }}"
        files, token = [], None
        while True:
            arguments = {"q": query, "fields": f"files({FILE_FIELDS},version,md5Checksum),nextPageToken", "pageSize": 1000}
            if token:
                arguments["pageToken"] = token
            result = drive.files().list(**arguments).execute()
            files.extend(result.get("files", []))
            token = result.get("nextPageToken")
            if not token:
                return drive, folders, files

    def _load(self, drive, metadata):
        # The scoped Drive query and account check still run on every read.
        # Drive's own content revision detects edits made outside this app too.
        fingerprint = (metadata.get("version"), metadata.get("md5Checksum"))
        # Jobs and worksheets contain large frozen bank snapshots; retaining
        # those duplicates can crowd out the model process during an import.
        cacheable = metadata.get("appProperties", {}).get("kind") == "state_banks" and all(fingerprint)
        file_id = metadata["id"]
        if cacheable:
            with self._record_cache_lock:
                cached = self._record_cache.get(file_id)
                if cached and cached[0] == fingerprint:
                    self._record_cache.move_to_end(file_id)
                    return deepcopy(cached[1])
        raw = drive.files().get_media(fileId=metadata["id"]).execute()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        value = json.loads(raw)
        value["drive_state_id"] = metadata["id"]
        if cacheable:
            size = _decoded_record_size(value)
            with self._record_cache_lock:
                previous = self._record_cache.pop(file_id, None)
                if previous:
                    self._record_cache_bytes -= previous[2]
                if size <= _RECORD_CACHE_LIMIT:
                    self._record_cache[file_id] = (fingerprint, deepcopy(value), size)
                    self._record_cache_bytes += size
                while len(self._record_cache) > 64 or self._record_cache_bytes > _RECORD_CACHE_LIMIT:
                    _, removed = self._record_cache.popitem(last=False)
                    self._record_cache_bytes -= removed[2]
        return value

    def read_record(self, kind, record_id):
        drive, _, files = self._find(kind, record_id)
        if len(files) > 1:
            raise DriveSyncError("같은 저장 기록이 둘 이상입니다. 관리자 확인이 필요합니다.", "ambiguous_record")
        return self._load(drive, files[0]) if files else None

    def list_records(self, kind):
        drive, _, files = self._find(kind)
        values = [self._load(drive, file) for file in files]
        if len({value["id"] for value in values}) != len(values):
            raise DriveSyncError("같은 저장 기록이 둘 이상입니다.", "ambiguous_record")
        return values

    def list_summaries(self, kind, summarize):
        # Always verify the account, scoped inventory and Drive's revision.
        # Only small projected rows are retained, never their bank snapshots.
        drive, _, files = self._find(kind)
        rows, identities = [], set()
        for metadata in files:
            key = (kind, metadata["id"])
            fingerprint = (metadata.get("version"), metadata.get("md5Checksum"))
            cached = None
            if all(fingerprint):
                with self._record_cache_lock:
                    previous = self._summary_cache.get(key)
                    if previous and previous[0] == fingerprint:
                        self._summary_cache.move_to_end(key)
                        cached = deepcopy(previous)
            if cached is None:
                value = self._load(drive, metadata)
                record_id, row = value["id"], summarize(value)
                entry = (fingerprint, record_id, deepcopy(row))
                size = _decoded_record_size(entry)
                with self._record_cache_lock:
                    previous = self._summary_cache.pop(key, None)
                    if previous:
                        self._summary_cache_bytes -= previous[3]
                    if all(fingerprint) and size <= _SUMMARY_CACHE_LIMIT:
                        self._summary_cache[key] = (*entry, size)
                        self._summary_cache_bytes += size
                    while len(self._summary_cache) > 512 or self._summary_cache_bytes > _SUMMARY_CACHE_LIMIT:
                        _, removed = self._summary_cache.popitem(last=False)
                        self._summary_cache_bytes -= removed[3]
                del value
            else:
                _, record_id, row, _ = cached
            if record_id in identities:
                raise DriveSyncError("같은 저장 기록이 둘 이상입니다.", "ambiguous_record")
            identities.add(record_id)
            if row is not None:
                rows.append(row)
        return rows

    def write_record(self, kind, value, previous=None):
        from .import_claim import assert_import_claim, guarded_import_write
        guarded = guarded_import_write(self, kind, value, previous)
        if guarded is not None:
            return guarded
        if kind == "banks":
            assert_import_claim(self)
        drive, _, folders = self._context()
        from googleapiclient.http import MediaIoBaseUpload
        content = {key: item for key, item in value.items() if key != "drive_state_id"}
        payload = json.dumps(content, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        media = MediaIoBaseUpload(io.BytesIO(payload), mimetype="application/json", resumable=False)
        properties = {"feature": FEATURE_TAG, "kind": f"state_{kind}", "record_key": _hash(value["id"]), "content_sha": hashlib.sha256(payload).hexdigest()}
        body = {"name": value["id"] + ".json", "appProperties": properties}
        existing_id = (previous or {}).get("drive_state_id")
        if existing_id:
            # The prior ID came from a scoped lookup, never from a user filename.
            result = drive.files().update(fileId=existing_id, body=body, media_body=media, fields="id").execute()
        else:
            body.update(mimeType="application/json", parents=[folders["states"]])
            result = drive.files().create(body=body, media_body=media, fields="id").execute()
        with self._record_cache_lock:
            previous_cache = self._record_cache.pop(result["id"], None)
            if previous_cache:
                self._record_cache_bytes -= previous_cache[2]
            previous_summary = self._summary_cache.pop((kind, result["id"]), None)
            if previous_summary:
                self._summary_cache_bytes -= previous_summary[3]
        return {**value, "drive_state_id": result["id"]}

    def prepare_record(self, kind, value, root):
        if kind == "banks":
            from .import_claim import assert_import_claim
            assert_import_claim(self)
        if kind == "jobs" and value.get("kind") in {"generate", "regenerate"}:
            # Generation checkpoints have no source/Sheets preparation. The
            # authoritative read and durable write still verify their context.
            return value
        sync = self._sync()
        if kind == "jobs" and value.get("kind") == "import":
            payload = value["payload"]
            if not payload.get("original_id"):
                path = root / "uploads" / payload["stored_name"]
                if not path.is_file() or _file_hash(path) != payload["sha256"]:
                    raise DriveSyncError("업로드 원본이 없거나 변경되었습니다.", "source_mismatch")
                mime = "application/pdf" if path.suffix.lower() == ".pdf" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                file = sync._upload(path, sync.vocab_folder_id, payload["filename"], "original_source", payload["sha256"], payload["sha256"], mime, None, immutable=True)
                payload.update(original_id=file["id"], original_link=sync._link(file))
        elif kind == "banks":
            construction = value.get('construction', {})
            if construction.get('engine_version') == 2 and construction.get('publishing_status') != 'complete':
                # Editable raw draft remains in durable state. Final Sheets
                # publication is owned by the v2 finalizer after validation.
                return value
            path = self.ensure_source(value, root)
            value["drive"] = sync.sync_bank(value, path, existing=value.get("drive", {}))
            value["storage"] = "drive"
            value["source"]["original_id"] = value["drive"]["original_id"]
        elif kind == "worksheets":
            value["drive"] = sync.sync_worksheet_record(value, existing=value.get("drive", {}))
            value["storage"] = "drive"
        return value

    def claim_import_step(self, job_id, lease_seconds=900):
        from .import_claim import claim_import_step
        return claim_import_step(self, job_id, lease_seconds)

    def release_import_step(self, job_id, token):
        from .import_claim import release_import_step
        return release_import_step(self, job_id, token)

    def ensure_source(self, record, root):
        source = record.get("source") or record.get("payload", {})
        stored_name = source.get("stored_name")
        if not isinstance(stored_name, str) or Path(stored_name).name != stored_name:
            raise DriveSyncError("저장된 원본 경로가 올바르지 않습니다.", "invalid_source")
        path = root / "uploads" / stored_name
        expected_hash = source.get("sha256")
        if path.is_file() and _file_hash(path) == expected_hash:
            return path
        original_id = source.get("original_id") or record.get("drive", {}).get("original_id")
        sync = self._sync()
        extension = Path(stored_name).suffix.lower()
        mime = "application/pdf" if extension == ".pdf" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        if original_id:
            original = sync._target(sync.vocab_folder_id, "original_source", expected_hash, mime, original_id)
        else:
            original = sync._target(sync.vocab_folder_id, "original_source", expected_hash, mime)
        if not original:
            raise DriveSyncError("Drive에 원본 파일이 없습니다.", "missing_source")
        raw = sync.drive.files().get_media(fileId=original["id"]).execute()
        if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != expected_hash:
            raise DriveSyncError("Drive 원본의 해시가 저장된 출처와 다릅니다.", "source_mismatch")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("." + path.name + ".pending")
        temporary.write_bytes(raw)
        temporary.replace(path)
        return path

    def ensure_pdf(self, worksheet, kind, root):
        path = root / "pdfs" / worksheet["id"] / f"{kind}.pdf"
        file_id = worksheet.get("drive", {}).get(f"{kind}_id")
        if not file_id:
            raise DriveSyncError("PDF를 먼저 생성하세요.", "missing_pdf")
        sync = self._sync()
        key = _hash(worksheet["id"])
        metadata = sync._target(sync.exam_folder_id, f"{kind}_pdf", key, "application/pdf", file_id)
        if path.is_file() and _file_hash(path) == metadata.get("appProperties", {}).get("content_sha"):
            return path
        raw = sync.drive.files().get_media(fileId=metadata["id"]).execute()
        if not isinstance(raw, bytes) or hashlib.sha256(raw).hexdigest() != metadata.get("appProperties", {}).get("content_sha"):
            raise DriveSyncError("Drive PDF의 해시가 저장 기록과 다릅니다.", "pdf_mismatch")
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".pending")
        try:
            temporary.write_bytes(raw)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def import_existing(self, file_id, root):
        drive, _, folders = self._context()
        metadata = drive.files().get(fileId=file_id, fields=FILE_FIELDS).execute()
        legacy_folder = self._folder(drive, "어휘 파일", folders["project"])
        allowed = {legacy_folder, folders["vocab"]}
        if not allowed.intersection(metadata.get("parents", [])) or metadata.get("trashed") or metadata.get("mimeType") not in {"application/pdf", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}:
            raise DriveSyncError("이 프로젝트의 PDF 또는 Excel 원본만 등록할 수 있습니다.", "invalid_source")
        raw = drive.files().get_media(fileId=file_id).execute()
        if not isinstance(raw, bytes) or len(raw) > 64 * 1024 * 1024:
            raise DriveSyncError("원본 파일을 가져올 수 없거나 크기가 64MB를 초과합니다.", "invalid_source")
        digest = hashlib.sha256(raw).hexdigest()
        extension = ".pdf" if metadata["mimeType"] == "application/pdf" else ".xlsx"
        path = root / "uploads" / (digest + extension)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return {"filename": metadata["name"], "sha256": digest, "stored_name": path.name, "legacy_original_id": file_id}

    def publish_pdfs(self, worksheet, result):
        worksheet["drive"] = self._sync().sync_worksheet(worksheet, result["student_path"], result["answer_path"], existing=worksheet.get("drive", {}), draft=bool(result.get("draft")))
        return worksheet

    def verify_bank_publication(self, bank):
        """Read the exact published tables before claiming a full import complete."""
        from .drive_sync import _bank_tables
        _, sheets = self._clients()
        spreadsheet_id = bank.get('drive', {}).get('bank_sheet_id')
        if not spreadsheet_id:
            raise DriveSyncError('저장된 단어장 시트를 확인하지 못했습니다.', 'missing_bank_sheet')
        tables = _bank_tables(bank)
        for row in tables['Metadata']:
            if row[0] == 'original_id':
                row[1] = bank.get('drive', {}).get('original_id', '')
            elif row[0] == 'original_link':
                row[1] = bank.get('drive', {}).get('links', {}).get('original', '')
        response = sheets.spreadsheets().values().batchGet(spreadsheetId=spreadsheet_id,
            ranges=list(tables), valueRenderOption='UNFORMATTED_VALUE').execute()
        values = response.get('valueRanges', [])
        if len(values) != len(tables):
            raise DriveSyncError('저장된 단어장 시트 범위가 다릅니다.', 'publication_mismatch')
        def normalized(rows):
            clean = []
            for row in rows:
                current = ['' if value is None else value for value in row]
                while current and current[-1] == '':
                    current.pop()
                clean.append(current)
            while clean and not clean[-1]:
                clean.pop()
            return clean
        for expected, observed in zip(tables.values(), values):
            if normalized(expected) != normalized(observed.get('values', [])):
                raise DriveSyncError('시트의 원문 또는 학습자료가 최종 저장값과 다릅니다.', 'publication_mismatch')
        return True

    def plan_worksheet_deletion(self, worksheet, jobs):
        from .deletion import plan_drive_deletion
        return plan_drive_deletion(self, worksheet, jobs)

    def execute_worksheet_deletion(self, file_ids):
        from .deletion import execute_drive_deletion
        return execute_drive_deletion(self, file_ids)


class ProductionFeatureService(FeatureService):
    work_mode = "request"

    def __init__(self, root, provider=None, ocr_engine=None, backend=None, import_provider=None, import_coordinator=None):
        self.backend = backend or OperatingDriveBackend()
        self.store = DriveJsonStore(root, lambda: self.backend)
        # Retain the injected attribute for compatibility; generation never calls it.
        self.provider = provider
        self.import_provider = import_provider
        self.ocr_engine = ocr_engine
        self.lock = threading.RLock()
        self.active = set()
        self.import_coordinator = import_coordinator

    def new_import_fields(self):
        if self.import_coordinator is not None:
            return self.import_coordinator.new_fields()
        return {}

    def read_job(self, job_id):
        job = self.get('jobs', job_id)
        if job.get('kind') == 'import' and job.get('engine_version') == 2:
            if self.import_coordinator is None:
                return {**job, 'status': 'interrupted', 'requires_attention': True,
                        'error_code': 'learning_worker_unavailable',
                        'message': '새 등록 작업자가 준비되지 않았습니다. 저장한 원문과 완료 자료는 유지됩니다.'}
            return self.import_coordinator.read_job(job_id)
        return job

    def status(self):
        _, _, folders = self.backend._context()
        return {"model_connected": False, "available_providers": [], "generation_mode": "script", "generation_available": True, "import_mode": "ai" if self.import_provider else "local_parser", "import_available": True, "storage": "drive", "feature": "word_meaning_test", "work_mode": self.work_mode, "drive_folder_url": f"https://drive.google.com/drive/folders/{folders['feature']}"}

    def dispatch(self, job):
        if job.get('kind') == 'import' and job.get('engine_version') == 2:
            if self.import_coordinator is None:
                raise HTTPException(503, '새 등록 작업자가 준비되지 않았습니다.')
            self.import_coordinator.enqueue_prepare(job)
        # The POST /step request owns all CPU until its checkpoint is durable.
        return job

    def ensure_source(self, record):
        return self.store.ensure_source(record)

    def ensure_pdf(self, worksheet, kind):
        return self.backend.ensure_pdf(worksheet, kind, self.store.root)

    def publish_pdfs(self, worksheet, result):
        return self.backend.publish_pdfs(worksheet, result)

    def import_existing(self, file_id):
        self.assert_new_import_allowed()
        payload = self.backend.import_existing(file_id, self.store.root)
        with self.lock:
            result = self.register_upload(payload["filename"], (self.store.root / "uploads" / payload["stored_name"]).read_bytes())
            if result.get("job_id"):
                job = self.get("jobs", result["job_id"])
                job["payload"].setdefault("legacy_original_ids", [])
                if file_id not in job["payload"]["legacy_original_ids"]:
                    job["payload"]["legacy_original_ids"].append(file_id)
                    self.store.put("jobs", job, expected_version=job["version"])
            elif result.get("bank_id"):
                bank = self.get("banks", result["bank_id"])
                bank["source"].setdefault("legacy_original_ids", [])
                if file_id not in bank["source"]["legacy_original_ids"]:
                    bank["source"]["legacy_original_ids"].append(file_id)
                    self.store.put("banks", bank, expected_version=bank["version"])
            return result

    def _import(self, job):
        super()._import(job)
        legacy_ids = job.get("payload", {}).get("legacy_original_ids", [])
        if legacy_ids:
            with self.lock:
                result = self.get("jobs", job["id"])
                bank = self.get("banks", result["result_id"])
                bank["source"]["legacy_original_ids"] = list(dict.fromkeys(bank["source"].get("legacy_original_ids", []) + legacy_ids))
                self.store.put("banks", bank, expected_version=bank["version"])

    def queue_regeneration(self, worksheet_id, question_id, version, model_options, request_id=None):
        payload = {"worksheet_id": worksheet_id, "question_id": question_id, "version": version, "model_options": {}}
        existing, job_id = self.find_intent("regenerate", request_id, payload)
        if existing:
            return {"job_id": existing["id"]}
        worksheet = self.get("worksheets", worksheet_id)
        if worksheet["version"] != version:
            raise HTTPException(409, "최신 내용을 다시 열어 주세요.")
        question = next((q for q in worksheet["questions"] if q["id"] == question_id), None)
        if not question:
            raise HTTPException(404, "문항을 찾을 수 없습니다.")
        if len(question.get("candidates", [])) >= 10:
            raise HTTPException(422, "더 이상 생성할 수 없습니다.")
        word = deepcopy(question.get("source_snapshot"))
        if not word:
            raise HTTPException(422, "재생성 원본이 없습니다.")
        word.update(source_word=question.get("source_word", word["word"]), word=question["word"])
        bank = self.worksheet_bank(worksheet)
        job = self.store.put("jobs", {"id": job_id, "kind": "regenerate", "generation_mode": "script", "status": "queued", "request_id": request_id, "request_payload": payload, "payload": payload, "selected_words": [word], "worksheet_snapshot": worksheet, "bank_snapshot": bank, "seed": int(uuid.uuid4().hex[:12], 16), "completed_batches": {}, "message": "스크립트 문항 재생성 대기"})
        return {"job_id": job["id"]}

    def retry_job(self, job_id):
        job = self.get("jobs", job_id)
        if job.get('kind') == 'import' and job.get('engine_version') == 2:
            if self.import_coordinator is None:
                raise HTTPException(503, '새 등록 작업자를 연결한 뒤 재시도해 주세요.')
            if self.read_job(job_id)['status'] not in {'failed', 'interrupted'}:
                raise HTTPException(409, '중단되거나 실패한 작업만 재시도할 수 있습니다.')
            self.import_coordinator.retry(job_id)
            return self.read_job(job_id)
        if job["status"] not in {"failed", "interrupted"}:
            raise HTTPException(409, "중단되거나 실패한 작업만 재시도할 수 있습니다.")
        job.update(status="queued", message="재시도 대기", in_flight=None)
        if job.get("kind") == "import":
            from .storage import now
            job.update(automatic_recovery_version=1, import_in_flight=None, import_recovery={},
                       recovery={"active": False, "attempt": 0}, requires_attention=False, started_at=now())
        return self.store.put("jobs", job)

    def _finish(self, job):
        payload = job["payload"]
        writing = payload.get("worksheet_kind") == "meaning_writing_test"
        complete_message = "뜻 쓰기 시험 생성 완료" if writing else "스크립트 시험지 생성 완료 · 검수 필요"
        questions = [q for key in sorted(job["completed_batches"], key=int) for q in job["completed_batches"][key]]
        if job["kind"] == "regenerate":
            worksheet = self.get("worksheets", payload["worksheet_id"])
            if job["id"] in worksheet.get("applied_regeneration_job_ids", []):
                return self.update_job(job["id"], status="complete", result_id=worksheet["id"], pending_batch=None, in_flight=None, phase_response=None, message="스크립트 시험지 생성 완료 · 검수 필요")
            if worksheet["version"] != payload["version"]:
                raise ConflictError("재생성 중 시험지가 수정되었습니다. 생성 결과는 작업 기록에 보존됩니다.")
            current = next(q for q in worksheet["questions"] if q["id"] == payload["question_id"])
            candidate = questions[0]
            candidate["source_snapshot"] = deepcopy(current.get("source_snapshot"))
            candidate["source_unit_labels"] = current.get("source_unit_labels", [])
            question = add_candidate(current, candidate)
            question = select_candidate(question, question["candidates"][-1]["id"])
            worksheet["questions"] = [question if q["id"] == question["id"] else q for q in worksheet["questions"]]
            worksheet["pdfs_stale"] = True
            worksheet.setdefault("generation_bank_snapshot", deepcopy(job["bank_snapshot"]))
            worksheet.setdefault("applied_regeneration_job_ids", []).append(job["id"])
            saved = self.store.put("worksheets", worksheet, expected_version=payload["version"])
        else:
            bank = job["bank_snapshot"]
            worksheet_id = "w_" + job["id"].removeprefix("j_")
            try:
                existing = self.store.get("worksheets", worksheet_id)
            except KeyError:
                existing = None
            if existing and existing.get("generation_job_id") == job["id"]:
                return self.update_job(job["id"], status="complete", result_id=existing["id"], pending_batch=None, in_flight=None, phase_response=None, message=complete_message)
            if writing:
                from .meaning_writing import create_writing_worksheet
                creator = create_writing_worksheet
            else:
                creator = create_worksheet
            worksheet = creator(bank, payload["unit_ids"], questions, title=payload.get("title"), selected_words=job["selected_words"], allow_partial=payload["allow_partial"])
            worksheet["id"] = "w_" + job["id"].removeprefix("j_")
            worksheet["generation_job_id"] = job["id"]
            worksheet["bank_id"] = payload["bank_id"]
            worksheet["generation_bank_snapshot"] = deepcopy(bank)
            worksheet["selection"] = {key: payload[key] for key in ("count", "shuffle", "seed", "allow_partial", "unit_ids")}
            worksheet["range_label"] = " · ".join(u["label"] for u in bank["units"] if u["id"] in payload["unit_ids"])
            validate_worksheet(worksheet, require_reviewed=False)
            # Unreviewed script output remains a visibly marked draft PDF.
            from .render import render_worksheet
            directory = self.store.root / "pdfs" / worksheet["id"]
            directory.mkdir(parents=True, exist_ok=True)
            result = render_worksheet(worksheet, directory / "student.pdf", directory / "answer.pdf")
            worksheet = self.publish_pdfs(worksheet, result)
            worksheet["pdfs"] = {kind: f"{PREFIX}/worksheets/{worksheet['id']}/pdf/{kind}" for kind in ("student", "answer")}
            worksheet["pdfs_stale"] = False
            saved = self.store.put("worksheets", worksheet)
        return self.update_job(job["id"], status="complete", result_id=saved["id"], progress={"completed": len(job["selected_words"]), "total": len(job["selected_words"])}, message=complete_message, pending_batch=None, in_flight=None, phase_response=None)

    def run_step(self, job_id):
        from .import_claim import ImportClaimLost
        with self.lock:
            if job_id in self.active:
                raise HTTPException(409, "이 작업은 처리 중입니다.")
            self.active.add(job_id)
        try:
            job = self.get("jobs", job_id)
            if job.get('kind') == 'import' and job.get('engine_version') == 2:
                # Browser compatibility only: durable tasks own generation.
                # A rollback must not feed v2 data to the legacy engine.
                return self.read_job(job_id)
            scoped = job.get("kind") != "import" or (job.get("automatic_recovery_version")
                and getattr(self.backend, "claim_import_step", None) is not None)
            scope = getattr(self.backend, "request_scope", nullcontext) if scoped else nullcontext
            with scope():
                return self._run_request_step(job_id, job)
        except ImportClaimLost:
            raise HTTPException(409, "다른 요청에서 이 작업을 처리 중입니다. 상태를 다시 확인합니다.") from None
        finally:
            with self.lock:
                self.active.discard(job_id)

    def _run_request_step(self, job_id, job):
        from .import_claim import ImportClaimLost
        claim_token = None
        claim_requested = False
        try:
            if job["status"] == "complete":
                return job
            if job["status"] in {"failed", "interrupted"}:
                return job
            if job["kind"] == "import":
                claim = getattr(self.backend, "claim_import_step", None)
                if job.get("automatic_recovery_version") and claim is not None:
                    claim_requested = True
                    claim_token = claim(job_id)
                    if claim_token is None:
                        current = self.get("jobs", job_id)
                        if current["status"] in {"complete", "failed", "interrupted"}:
                            return current
                        raise HTTPException(409, "다른 요청에서 이 작업을 처리 중입니다. 상태를 다시 확인합니다.")
                    # The atomic claim may have raced with a prior worker's
                    # completed checkpoint. Only this fresh snapshot is owned.
                    job = self.get("jobs", job_id)
                    if job["status"] in {"complete", "failed", "interrupted"}:
                        return job
                if self.import_provider is not None:
                    return self._import_step(job)
                self.ensure_source(job)
                self._import(job)
                return self.get("jobs", job_id)
            return self._generation_step(job_id, job)
        except ImportClaimLost:
            # A delayed worker must not convert ownership loss into a recovery
            # write that could overwrite the new worker's durable checkpoint.
            raise
        except HTTPException:
            raise
        except Exception as error:
            if claim_requested and claim_token is None:
                # Ownership is unknown after a claim acknowledgement failure.
                # A later GET/step can reconcile it without an unowned write.
                raise HTTPException(503, "작업 상태를 다시 확인한 뒤 자동으로 계속합니다.") from None
            recovered = self._recover_import_error(job_id, error)
            if recovered is not None:
                return recovered
            job = self.get("jobs", job_id)
            public = str(error) if isinstance(error, (ValueError, ConflictError)) else "처리를 완료하지 못했습니다. 저장된 작업에서 재시도하세요."
            changes = {"status": "failed", "error_code": getattr(error, "code", "processing_failed"), "message": public[:1000], "in_flight": None, "phase_response": None,
                       "import_in_flight": None}
            return self.update_job(job_id, **changes)
        finally:
            if claim_token is not None:
                self.backend.release_import_step(job_id, claim_token)


    def _generation_step(self, job_id, job):
        writing = job["payload"].get("worksheet_kind") == "meaning_writing_test"
        if "selected_words" not in job:
            bank = self.get("banks", job["payload"]["bank_id"])
            payload = job["payload"]
            words = select_words(bank, payload["unit_ids"], count=payload["count"], shuffle=True if writing else payload["shuffle"], seed=payload["seed"], allow_partial=payload["allow_partial"])
            job = self.update_job(job_id, selected_words=words, bank_snapshot=bank, completed_batches={})
        if job.get("generation_mode") != "script":
            # Freeze the old job's selected source while discarding AI phases.
            bank = job.get("bank_snapshot")
            if bank is None:
                worksheet = job.get("worksheet_snapshot") or self.get("worksheets", job["payload"]["worksheet_id"])
                bank = self.worksheet_bank(worksheet)
            job = self.update_job(job_id, generation_mode="script", bank_snapshot=bank, completed_batches={}, pending_batch=None, phase_response=None, in_flight=None, payload=self.script_params(job["payload"]), seed=job.get("seed", int(uuid.uuid4().hex[:12], 16)), error_code=None)
        words = job["selected_words"]
        done = job.get("completed_batches", {})
        batch_index = next((i for i in range((len(words) + 11) // 12) if str(i) not in done), None)
        if batch_index is None:
            return self._finish(job)
        source_batch = words[batch_index * 12:(batch_index + 1) * 12]
        self.update_job(job_id, status="running", progress={"completed": batch_index * 12, "total": len(words), "stage": "script"}, message="스크립트 문항 생성 중")
        if writing:
            from .meaning_writing import generate_writing_questions
            generator = generate_writing_questions
        else:
            generator = generate_script_questions
        generated = generator(source_batch, job["bank_snapshot"], seed=job["payload"].get("seed", job.get("seed")))
        for question, word in zip(generated, source_batch):
            question["source_snapshot"] = deepcopy(word)
            question["source_unit_labels"] = [u["label"] for u in job["bank_snapshot"].get("units", []) if u["id"] in word["unit_ids"]]
        done[str(batch_index)] = generated
        return self.update_job(job_id, completed_batches=done, pending_batch=None, phase_response=None, in_flight=None, status="queued", progress={"completed": min((batch_index + 1) * 12, len(words)), "total": len(words), "stage": "script"}, message="다음 처리 대기")


def create_production_service(local_root):
    """Safe to call during app import: no credential/network access at boot."""
    from .providers import OperatingProvider
    return ProductionFeatureService(Path(local_root), import_provider=OperatingProvider())
