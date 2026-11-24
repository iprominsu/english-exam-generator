"""Project-local, atomic persistence with optimistic concurrency.

Cloud integrations can replace this store without changing vocabulary/exam logic.
No credentials, raw conversation archives, or unrelated project files are read.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def _atomic_replace(source, destination):
    """Allow a short Windows/OneDrive sharing-lock window, then fail normally."""
    for attempt in range(5):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.02)


class StoreError(ValueError):
    pass


class ConflictError(StoreError):
    pass


class JsonStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def path(self, kind, record_id):
        if kind not in {"banks", "jobs", "worksheets", "uploads", "pdfs"}:
            raise StoreError("잘못된 저장 종류입니다.")
        if not isinstance(record_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", record_id):
            raise StoreError("잘못된 항목 ID입니다.")
        return self.root / kind / f"{record_id}.json"

    def get(self, kind, record_id):
        with self._lock:
            path = self.path(kind, record_id)
            if not path.is_file():
                raise KeyError(record_id)
            return json.loads(path.read_text(encoding="utf-8"))

    def list(self, kind):
        self.path(kind, "validate")
        with self._lock:
            directory = self.root / kind
            if not directory.exists():
                return []
            return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]

    def put(self, kind, record, expected_version=None):
        with self._lock:
            value = copy.deepcopy(record)
            path = self.path(kind, value["id"])
            previous = self.get(kind, value["id"]) if path.exists() else None
            version = previous.get("version", 0) if previous else 0
            if expected_version is not None and expected_version != version:
                raise ConflictError("다른 곳에서 수정되었습니다. 최신 내용을 다시 열어 주세요.")
            value.setdefault("created_at", previous.get("created_at") if previous else now())
            value["updated_at"] = now()
            value["version"] = version + 1
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
            try:
                with temporary.open("w", encoding="utf-8", newline="\n") as stream:
                    json.dump(value, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.flush()
                    os.fsync(stream.fileno())
                _atomic_replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
            return value

    def recover_jobs(self):
        for job in self.list("jobs"):
            if job.get("status") in {"queued", "running"}:
                job.update(status="interrupted", message="작업이 중단되었습니다. 재시도로 이어서 처리할 수 있습니다.")
                self.put("jobs", job)

    def put_metadata(self, kind, record, expected_version=None):
        return self.put(kind, record, expected_version=expected_version)

    def delete(self, kind, record_id):
        with self._lock:
            self.path(kind, record_id).unlink(missing_ok=True)


class DriveJsonStore(JsonStore):
    """Drive is authoritative; disk holds only replaceable request-local caches.

    backend_factory is lazy and supplies read_record/list_records/write_record,
    prepare_record and ensure_source. Boot performs no network calls. Optimistic
    versions are checked against Drive, never against a possibly stale cache.
    """
    def __init__(self, root, backend_factory):
        super().__init__(root)
        self.backend_factory = backend_factory

    def _mirror(self, kind, record):
        path = self.path(kind, record["id"])
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
            _atomic_replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def get(self, kind, record_id):
        self.path(kind, record_id)
        # An unrelated slow list read must not block a bank/chapter selection.
        # Reads are authoritative snapshots; the write lock/version check remains
        # in _put_record. Atomic mirrors are disposable and never used as truth.
        value = self.backend_factory().read_record(kind, record_id)
        if value is None:
            raise KeyError(record_id)
        self._cache(kind, value)
        return copy.deepcopy(value)

    def list(self, kind):
        self.path(kind, "validate")
        values = self.backend_factory().list_records(kind)
        for value in values:
            self._cache(kind, value)
        return copy.deepcopy(values)

    def list_summaries(self, kind, summarize):
        """List public rows without mirroring or retaining large snapshots."""
        self.path(kind, "validate")
        backend = self.backend_factory()
        if hasattr(backend, "list_summaries"):
            return backend.list_summaries(kind, summarize)
        return [row for value in self.list(kind) if (row := summarize(value)) is not None]

    def put(self, kind, record, expected_version=None):
        return self._put_record(kind, record, expected_version, prepare=True)

    def put_metadata(self, kind, record, expected_version=None):
        return self._put_record(kind, record, expected_version, prepare=False)

    def _put_record(self, kind, record, expected_version=None, *, prepare=True):
        value = copy.deepcopy(record)
        self.path(kind, value["id"])
        with self._lock:
            backend = self.backend_factory()
            previous = backend.read_record(kind, value["id"])
            version = previous.get("version", 0) if previous else 0
            if expected_version is not None and expected_version != version:
                raise ConflictError("다른 곳에서 수정되었습니다. 최신 내용을 다시 열어 주세요.")
            value.setdefault("created_at", previous.get("created_at") if previous else now())
            value["updated_at"] = now()
            value["version"] = version + 1
            assert_owned = getattr(backend, 'assert_bank_write_ownership', None) if kind == 'banks' else None
            if assert_owned is not None:
                assert_owned()
            if prepare:
                value = backend.prepare_record(kind, value, self.root)
            if assert_owned is not None:
                assert_owned()
            value = backend.write_record(kind, value, previous)
            self._cache(kind, value)
            return copy.deepcopy(value)

    def _cache(self, kind, value):
        try:
            self._mirror(kind, value)
        except OSError as error:
            # The durable operation already succeeded. A disposable cache
            # failure must not turn that success into a retry of an AI call.
            logging.getLogger(__name__).warning("Project-local Drive cache unavailable (%s)", type(error).__name__)

    def ensure_source(self, record):
        return self.backend_factory().ensure_source(record, self.root)

    def recover_jobs(self):
        # Unknown in-flight AI work is handled when run_step explicitly sees
        # the checkpoint. Boot must not enumerate Drive or modify job records.
        return None

