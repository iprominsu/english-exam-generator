"""Durable, versioned supplemental-learning jobs. No cloud clients at import.

The state transaction never calls AI, Drive, or Cloud Tasks. Providers receive
copies of immutable batch inputs; only validated learning fields are retained.
Production supplies Firestore plus an authenticated worker and reconciler.
"""
from __future__ import annotations

from copy import deepcopy
from contextlib import contextmanager
from dataclasses import asdict, dataclass
import hashlib
import json
import re
import threading
import time
import uuid


ENGINE_VERSION = 2
PREPARATION_POLICY = 'before_review_v1'
MAX_DOCUMENT_BYTES = 900_000  # Leave room for Firestore field-name overhead.
TERMINAL = frozenset({"complete", "failed", "superseded", "cancelled"})


class EngineError(RuntimeError):
    status = 422


class WorkBusy(EngineError):
    status = 503


class LeaseLost(WorkBusy):
    pass


class SourceChanged(EngineError):
    status = 409


class SourceReviewRequired(SourceChanged):
    code = "source_review_required"


class LearningReviewRequired(EngineError):
    """A durable quality hold; no automatic retry can approve source changes."""
    def __init__(self, code='learning_quality_review', entry_ids=()):
        super().__init__('학습자료를 확인해야 합니다. 원문과 저장한 수정값은 유지됩니다.')
        self.code = code if code in {'learning_quality_review', 'learning_source_conflict'} else 'learning_quality_review'
        self.entry_ids = list(entry_ids)


def assert_source_ready(bank):
    if (not bank.get("entries") or bank.get("page_issues")
            or any(e.get("status") != "ready" for e in bank["entries"])
            or any(u.get("status") == "review" for u in bank.get("units", []))):
        raise SourceReviewRequired("원문 확인 상태가 바뀌었습니다. 확인을 마친 뒤 작업을 재시도해 주세요.")


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _bounded(value):
    if len(_bytes(value)) > MAX_DOCUMENT_BYTES:
        raise EngineError("학습 작업 기록이 저장 한도를 초과합니다.")
    return value


def _firestore_document(value):
    """Reject Standard-edition nested arrays before submitting any writes."""
    def visit(item, array_element=False):
        if isinstance(item, (list, tuple)):
            if array_element:
                raise EngineError("학습 기록에 저장할 수 없는 중첩 배열이 있습니다.")
            for child in item:
                visit(child, True)
        elif isinstance(item, dict):
            for child in item.values():
                visit(child)
    visit(value)
    return _bounded(value)


def _identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value):
        raise EngineError("학습 작업 ID가 올바르지 않습니다.")
    return value


def input_fingerprint(entry):
    from .source_integrity import learning_input_fingerprint
    return learning_input_fingerprint(entry)


def generation_eligible(entry):
    """Human confirmation is separate from a usable, non-excluded AI input."""
    return (entry.get('status') in {'ready', 'review'}
        and isinstance(entry.get('word'), str) and bool(entry['word'].strip())
        and isinstance(entry.get('meaning'), str) and bool(entry['meaning'].strip()))


def before_review(value):
    return (value.get('preparation_policy')
        or value.get('construction', {}).get('preparation_policy')) == PREPARATION_POLICY


def generation_version(bank, *, preparation_policy=None):
    """Names do not invalidate learning; current words/meanings and raw source do."""
    policy = preparation_policy or bank.get('construction', {}).get('preparation_policy')
    value = {"source": bank.get("source_manifest", {}).get("sha256"),
             "entries": [input_fingerprint(e) for e in bank.get("entries", [])]}
    if policy == PREPARATION_POLICY:
        value['generation_targets'] = [generation_eligible(e) for e in bank.get('entries', [])]
    return digest(value)


@dataclass(frozen=True)
class LearningSpec:
    model: str = "gpt-6.1-sol"
    reasoning: str = "high"
    batch_size: int = 8
    material_version: str = "definition1-examples2-passage1-v1"
    prompt_version: str = "referenced-learning-v2-quality3"
    validator_version: str = "referenced-learning-v2-quality3"

    def __post_init__(self):
        if self.model != "gpt-6.1-sol" or self.reasoning != "high" or self.batch_size != 8:
            raise EngineError("학습 생성 규격은 GPT-6.1 Sol / High / 8개 묶음입니다.")
        if any(not isinstance(v, str) or not v for v in (self.material_version, self.prompt_version, self.validator_version)):
            raise EngineError("학습자료 규격 버전을 확인해 주세요.")


def build_manifest(bank, source_version=None, *, spec=None, preparation_policy=None):
    from .source_integrity import assert_source_manifest
    assert_source_manifest(bank)
    source_manifest = bank.get("source_manifest") or {}
    if source_manifest.get("evidence") == "legacy_unverified":
        raise EngineError("원문 확인을 마친 후 전체 학습자료를 생성해 주세요.")
    entries = bank.get("entries") or []
    policy = preparation_policy or bank.get('construction', {}).get('preparation_policy')
    if policy != PREPARATION_POLICY:
        assert_source_ready(bank)
    elif (not entries or any(e.get('status') not in {'ready', 'review', 'excluded'} for e in entries)):
        raise EngineError('생성할 원문 항목을 확인할 수 없습니다.')
    ids = [e.get("id") for e in entries]
    if any(not isinstance(v, str) or not v for v in ids) or len(set(ids)) != len(ids):
        raise EngineError("원문 항목 ID가 없거나 중복되었습니다.")
    current_version = generation_version(bank, preparation_policy=policy or 'after_review_v1')
    if source_version is not None and source_version != current_version:
        raise SourceChanged("학습 생성 기준이 변경되었습니다.")
    clean = deepcopy(bank)
    # Existing supplemental output is never trusted as source or a cache entry.
    for entry in clean["entries"]:
        for key in ("learning", "learning_pending", "learning_stale", "learning_available", "learning_input_fingerprint", 'learning_review'):
            entry.pop(key, None)
    manifest = {"engine_version": ENGINE_VERSION, "bank": clean,
                "source_version": current_version, "spec": asdict(spec or LearningSpec()),
                "input_fingerprints": [input_fingerprint(e) for e in clean["entries"]]}
    if policy == PREPARATION_POLICY:
        manifest['preparation_policy'] = policy
    manifest["sha256"] = digest(manifest)
    return manifest


def cache_key(entry, spec):
    # Exact source values and all relevant context; IDs/locations are rebound by
    # the validator so identical content can be reused in another source file.
    source = entry.get("source") or {}
    context = {k: v for k, v in source.items() if k not in {
        "page", "bbox", "row", "column", "sheet", "sheet_name", "line", "format"}}
    return digest({"word": entry.get("word"), "meaning": entry.get("meaning"),
                   "related": entry.get("related", {}), "source_context": context,
                   "lexical_context": {k: entry.get(k) for k in ("pos", "part_of_speech", "context")}, "spec": spec})


@contextmanager
def _heartbeat_loop(interval, callback, *, name, on_error=None, enabled=True):
    """백그라운드 데몬 스레드로 리스 갱신을 반복 실행하고 종료 시 안전하게 합류한다."""
    if not enabled:
        yield
        return
    stop = threading.Event()
    def maintain():
        while not stop.wait(interval):
            try:
                callback()
            except Exception:
                if on_error is not None:
                    on_error()
                return
    thread = threading.Thread(target=maintain, daemon=True, name=name)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)


class MemoryStateStore:
    """Deterministic offline transaction store; never used as production fallback."""
    durable = False

    def __init__(self):
        self.lock = threading.RLock()
        self.documents = {}

    def get(self, key):
        with self.lock:
            return deepcopy(self.documents.get(key))

    def scan(self, prefix):
        with self.lock:
            return [(k, deepcopy(v)) for k, v in sorted(self.documents.items()) if k.startswith(prefix)]

    def atomic(self, keys, operation):
        with self.lock:
            writes, result = operation({k: deepcopy(self.documents.get(k)) for k in keys})
            for key, value in writes.items():
                if key not in keys:
                    raise AssertionError("Transaction wrote an unread key")
                if value is not None:
                    _bounded(value)
            for key, value in writes.items():
                if value is None:
                    self.documents.pop(key, None)
                else:
                    self.documents[key] = deepcopy(value)
            return deepcopy(result)


class FirestoreStateStore:
    """Small documents with all reads before writes and SDK conflict retries."""
    durable = True

    def __init__(self, client, *, collection="melody_learning_v2", transaction_runner=None,
                 account_collection="melody_learning_account_slots"):
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,99}", collection):
            raise ValueError("Invalid state collection")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,99}", account_collection):
            raise ValueError("Invalid account collection")
        self.client, self.collection = client, client.collection(collection)
        self.account_collection = client.collection(account_collection)
        self.transaction_runner = transaction_runner

    def _ref(self, key):
        collection = self.account_collection if key.startswith(("account/", "lease/")) else self.collection
        return collection.document(hashlib.sha256(key.encode()).hexdigest())

    @staticmethod
    def _decode(snapshot):
        if not snapshot.exists:
            return None
        value = snapshot.to_dict()
        value.pop("_key", None)
        return value

    def get(self, key):
        return self._decode(self._ref(key).get())

    def scan(self, prefix):
        query = self.collection.where("_key", ">=", prefix).where("_key", "<", prefix + "\uf8ff")
        return [(snap.to_dict()["_key"], self._decode(snap)) for snap in query.stream()]

    def atomic(self, keys, operation):
        keys = tuple(dict.fromkeys(keys))
        refs = {key: self._ref(key) for key in keys}
        def run(transaction):
            values = {key: self._decode(ref.get(transaction=transaction)) for key, ref in refs.items()}
            writes, result = operation(values)
            for key, value in writes.items():
                if key not in refs:
                    raise AssertionError("Transaction wrote an unread key")
                if value is None:
                    transaction.delete(refs[key])
                else:
                    transaction.set(refs[key], _firestore_document({**value, "_key": key}))
            return result
        if self.transaction_runner is not None:
            return self.transaction_runner(run)
        from google.cloud import firestore
        return firestore.transactional(run)(self.client.transaction())


class DurableLease:
    def __init__(self, store, key, token, clock):
        self.store, self.key, self.token, self.clock = store, key, token, clock
        self.cancelled = threading.Event()

    def assert_owned(self):
        value = self.store.get(self.key)
        if self.cancelled.is_set() or not value or value.get("token") != self.token or value.get("expires_at", 0) <= self.clock():
            self.cancelled.set()
            raise LeaseLost("공유 저장 실행 소유권이 변경되었습니다.")


@contextmanager
def distributed_lease(store, key, *, lease_seconds=900, heartbeat_seconds=30, clock=time.time):
    """Short transaction plus renewable ownership around external publication.

    Call lease.assert_owned() immediately before each write; a context exit
    cannot undo an already accepted remote request.
    """
    if not key.startswith("lease/") or not 30 <= lease_seconds <= 1800 or not 1 <= heartbeat_seconds < lease_seconds / 2:
        raise ValueError("Invalid shared lease")
    token = uuid.uuid4().hex
    handle = DurableLease(store, key, token, clock)
    def acquire(values):
        existing = values[key]
        if existing and existing.get("expires_at", 0) > clock():
            raise WorkBusy("같은 단어장을 다른 곳에서 저장하고 있습니다.")
        return {key: {"token": token, "expires_at": clock() + lease_seconds}}, None
    store.atomic([key], acquire)
    def renew(values):
        value = values[key]
        if not value or value.get("token") != token or value.get("expires_at", 0) <= clock():
            raise LeaseLost("공유 저장 실행 상태가 바뀌었습니다.")
        value["expires_at"] = clock() + lease_seconds
        return {key: value}, None
    try:
        with _heartbeat_loop(heartbeat_seconds, lambda: store.atomic([key], renew),
                             name="bank-write-lease", on_error=handle.cancelled.set):
            yield handle
            handle.assert_owned()
    finally:
        def release(values):
            value = values[key]
            return ({key: None} if value and value.get("token") == token else {}), None
        store.atomic([key], release)


@contextmanager
def account_generation_slot(store, account_key, job_id, *, lease_seconds=900, heartbeat_seconds=30, clock=time.time):
    """Shared limit for source preparation and legacy generation as well as batches."""
    if not account_key or not 30 <= lease_seconds <= 1800 or not 1 <= heartbeat_seconds < lease_seconds / 2:
        raise ValueError("Invalid shared account slot configuration")
    key, token = "account/" + digest(account_key), uuid.uuid4().hex
    cancelled = threading.Event()
    def acquire(values):
        account = values[key] or {"slots": []}
        account["slots"] = [slot for slot in account["slots"] if slot["expires_at"] > clock()]
        if len(account["slots"]) >= 2:
            raise WorkBusy("다른 학습자료를 생성 중입니다.")
        account["slots"].append({"token": token, "expires_at": clock() + lease_seconds,
                                 "job_id": job_id, "batch_id": "source"})
        return {key: account}, None
    store.atomic([key], acquire)
    def renew(values):
        account = values[key]
        slot = next((s for s in account["slots"] if s["token"] == token), None)
        if slot is None or slot["expires_at"] <= clock():
            raise LeaseLost("계정 실행 슬롯이 변경되었습니다.")
        slot["expires_at"] = clock() + lease_seconds
        return {key: account}, None
    try:
        with _heartbeat_loop(heartbeat_seconds, lambda: store.atomic([key], renew),
                             name="source-account-lease", on_error=cancelled.set):
            yield cancelled
            if cancelled.is_set():
                raise LeaseLost("계정 실행 소유권을 확인할 수 없습니다.")
    finally:
        def release(values):
            account = values[key]
            if not account:
                return {}, None
            account["slots"] = [s for s in account["slots"] if s["token"] != token]
            return {key: account}, None
        store.atomic([key], release)


class LearningEngine:
    """Callbacks: provider(entries, cancel_event=Event), validate(inputs, outputs),
    current_source_version(bank_id), publish(manifest, entries, job_id), readback(receipt).
    Publish must use expected source version and the job_id as an idempotency key.
    """
    def __init__(self, store, *, provider, validate, current_source_version, publish, readback,
                 clock=time.time, lease_seconds=900, heartbeat_seconds=30, max_attempts=6, reuse=True,
                 rebind_cache=None, current_bank=None, publication_context=None, versioned_provider=False):
        if not 30 <= lease_seconds <= 1800 or not 1 <= heartbeat_seconds < lease_seconds / 2:
            raise ValueError("Invalid worker lease settings")
        self.store, self.provider, self.validate = store, provider, validate
        self.current_source_version, self.publish, self.readback = current_source_version, publish, readback
        self.clock, self.lease_seconds, self.heartbeat_seconds = clock, lease_seconds, heartbeat_seconds
        self.max_attempts, self.reuse = max_attempts, reuse
        self.rebind_cache = rebind_cache
        self.current_bank = current_bank
        self.publication_context = publication_context
        self.versioned_provider = versioned_provider

    @staticmethod
    def _job(job_id):
        return "job/" + _identifier(job_id)

    @staticmethod
    def _batch(job_id, batch_id):
        return "batch/" + _identifier(job_id) + "/" + _identifier(batch_id)

    @staticmethod
    def _account(account_key):
        return "account/" + digest(account_key)

    def create_job(self, job_id, manifest, account_key, *, received_at=None, commit_guard=None):
        if not isinstance(account_key, str) or not account_key:
            raise EngineError("생성 계정의 공통 제한 키가 필요합니다.")
        body = deepcopy(manifest)
        expected = body.pop("sha256", None)
        if expected != digest(body):
            raise EngineError("원문 기준표가 변경되었습니다.")
        LearningSpec(**body["spec"])
        bank, entries = body["bank"], body["bank"]["entries"]
        source_rows = {e["id"]: {"index": index, "entry": e} for index, e in enumerate(bank["source_manifest"]["entries"])}
        if generation_version(bank, preparation_policy=body.get('preparation_policy', 'after_review_v1')) != body["source_version"]:
            raise SourceChanged("원문 기준표 입력이 일치하지 않습니다.")
        batches = []
        for offset in range(0, len(entries), 8):
            subset = deepcopy(entries[offset:offset + 8])
            batch_id = "b_" + str(offset // 8).zfill(5)
            batches.append({"id": batch_id, "job_id": job_id, "state": "pending", "inputs": subset,
                            "input_hash": digest(subset), "attempts": 0, "next_attempt_at": 0,
                            "source_rows": [source_rows[e["id"]] for e in subset],
                            "dispatch_generation": 0, "enqueued_generation": -1, "claim": None})
        if not batches:
            raise EngineError("학습할 원문 항목이 없습니다.")
        # Batch manifests are independently immutable so large banks never need
        # to fit in a single Firestore document or be rewritten per batch.
        header = deepcopy(body)
        header["bank"].pop("entries")
        header["bank"]["source_manifest"].pop("entries")
        header.pop("input_fingerprints", None)
        header["sha256"] = expected
        job = {"id": job_id, "state": "pending", "engine_version": ENGINE_VERSION,
               "manifest": header, "account_key": self._account(account_key),
               "batch_ids": [b["id"] for b in batches],
               "total": sum(generation_eligible(e) for e in entries) if before_review(body) else len(entries),
               "completed_entries": 0, "completed_batches": 0,
               "created_at": self.clock() if received_at is None else float(received_at), "claim": None}
        key = self._job(job_id)
        # A job can have many batches. Stage individually under deterministic
        # keys; workers cannot run until the final job record is committed.
        existing = self.store.get(key)
        if existing:
            if existing["manifest"]["sha256"] != expected or existing["account_key"] != job["account_key"]:
                raise SourceChanged("같은 작업 ID에 다른 입력을 등록할 수 없습니다.")
            return self.status(job_id)
        for batch in batches:
            batch_key = self._batch(job_id, batch["id"])
            def stage(values, batch=batch, batch_key=batch_key):
                previous = values[batch_key]
                if previous and previous["input_hash"] != batch["input_hash"]:
                    raise SourceChanged("학습 묶음 입력이 변경되었습니다.")
                return ({} if previous else {batch_key: batch}), None
            self.store.atomic([batch_key], stage)
        def commit(values):
            if commit_guard is not None:
                commit_guard[1](values[commit_guard[0]])
            previous = values[key]
            if previous and previous["manifest"]["sha256"] != expected:
                raise SourceChanged("같은 작업 ID에 다른 입력을 등록할 수 없습니다.")
            return ({} if previous else {key: job}), None
        self.store.atomic([key, *([commit_guard[0]] if commit_guard is not None else [])], commit)
        return self.status(job_id)

    def status(self, job_id):
        job = self.store.get(self._job(job_id))
        if not job:
            raise KeyError(job_id)
        return {"id": job_id, "engine_version": ENGINE_VERSION, "state": job["state"],
                "completed": job["completed_entries"],
                "total": job["total"], "pending_batches": len(job["batch_ids"]) - job["completed_batches"],
                "source_version": job["manifest"]["source_version"],
                "error_code": job.get("error_code"), "receipt": job.get("receipt"),
                "review_entries": deepcopy(job.get('review_entries', {}))}

    def _assert_current(self, job, batch=None):
        bank_id = job["manifest"]["bank"]["id"]
        current = self.current_bank(bank_id) if self.current_bank is not None else None
        policy = job['manifest'].get('preparation_policy', 'after_review_v1')
        version = generation_version(current, preparation_policy=policy) if current is not None else self.current_source_version(bank_id)
        error = None
        if current is not None:
            checked = current
            if batch is not None:
                ids = {e["id"] for e in batch["inputs"]}
                checked = {**current, "entries": [e for e in current.get("entries", []) if e["id"] in ids]}
                units = {e["unit_id"] for e in checked["entries"]}
                checked["units"] = [u for u in current.get("units", []) if u["id"] in units]
            try:
                if before_review(job['manifest']):
                    from .source_integrity import assert_source_manifest
                    assert_source_manifest(current)
                else:
                    assert_source_ready(checked)
            except (SourceReviewRequired, ValueError) as changed:
                error = changed
        if error is None and version == job["manifest"]["source_version"]:
            return
        if error is None and batch is not None and current is not None:
            by_id = {e["id"]: e for e in current.get("entries", [])}
            if (current.get("source_manifest", {}).get("sha256") == job["manifest"]["bank"]["source_manifest"]["sha256"]
                    and all(e["id"] in by_id and input_fingerprint(e) == input_fingerprint(by_id[e["id"]])
                        and (not before_review(job['manifest']) or generation_eligible(e) == generation_eligible(by_id[e['id']]))
                        for e in batch["inputs"])):
                return
        error = error or SourceChanged("학습 생성 중 원문이 수정되어 이전 결과를 적용하지 않았습니다.")
        job_key = self._job(job["id"])
        batch_key = self._batch(job["id"], batch["id"]) if batch is not None else None
        def hold(values):
            saved = values[job_key]
            if not saved or saved["state"] == "complete":
                return {}, None
            code = getattr(error, "code", "source_changed")
            saved.update(state="needs_review" if current is not None else "superseded", error_code=code)
            writes = {job_key: saved}
            if batch_key is not None and values[batch_key]["state"] != "complete":
                value = values[batch_key]
                value.update(state="superseded", error_code=code)
                writes[batch_key] = value
            return writes, None
        self.store.atomic([job_key, *([batch_key] if batch_key else [])], hold)
        raise error

    def _claim(self, job_id, batch_id):
        job_key, batch_key = self._job(job_id), self._batch(job_id, batch_id)
        before = self.store.get(job_key)
        if not before or batch_id not in before["batch_ids"]:
            raise KeyError(batch_id)
        before_batch = self.store.get(batch_key)
        if before_batch["state"] == "complete":
            return None  # A late duplicate must ACK its immutable completion.
        self._assert_current(before, before_batch)
        account_key, token, now = before["account_key"], uuid.uuid4().hex, self.clock()
        def take(values):
            job, batch = values[job_key], values[batch_key]
            if batch["state"] == "complete":
                return {}, None
            if batch['state'] == 'needs_review':
                raise LearningReviewRequired(batch.get('error_code'), batch.get('review_entry_ids', []))
            if job["state"] in TERMINAL:
                raise SourceChanged("종료된 작업은 다시 실행할 수 없습니다.")
            if batch["next_attempt_at"] > now or (batch.get("claim") or {}).get("expires_at", 0) > now:
                raise WorkBusy("학습 묶음이 진행 중이거나 재시도를 기다리고 있습니다.")
            account = values[account_key] or {"slots": []}
            account["slots"] = [s for s in account["slots"] if s["expires_at"] > now]
            if len(account["slots"]) >= 2:
                raise WorkBusy("다른 학습자료를 생성 중입니다.")
            if batch["attempts"] >= self.max_attempts:
                raise EngineError("학습자료 생성 재시도 한도에 도달했습니다.")
            claim = {"token": token, "expires_at": now + self.lease_seconds, "job_id": job_id, "batch_id": batch_id}
            account["slots"].append(claim)
            batch.update(state="running", claim=claim, attempts=batch["attempts"] + 1, started_at=now)
            if job["state"] != "needs_review":
                job["state"] = "running"
            return {job_key: job, batch_key: batch, account_key: account}, (job, batch, token)
        return self.store.atomic([job_key, batch_key, account_key], take)

    def _owned(self, batch, token):
        claim = batch.get("claim") or {}
        if claim.get("token") != token or claim.get("expires_at", 0) <= self.clock():
            raise LeaseLost("학습 묶음의 실행 소유권이 변경되었습니다.")

    def _heartbeat(self, job, batch_id, token):
        batch_key, account_key = self._batch(job["id"], batch_id), job["account_key"]
        def renew(values):
            batch, account = values[batch_key], values[account_key]
            self._owned(batch, token)
            slot = next((s for s in account["slots"] if s["token"] == token), None)
            if slot is None or slot["expires_at"] <= self.clock():
                raise LeaseLost("계정 실행 슬롯이 변경되었습니다.")
            slot["expires_at"] = batch["claim"]["expires_at"] = self.clock() + self.lease_seconds
            return {batch_key: batch, account_key: account}, None
        self.store.atomic([batch_key, account_key], renew)

    def _validated(self, inputs, outputs, spec=None):
        options = {'spec': deepcopy(spec)} if self.versioned_provider else {}
        result = self.validate(deepcopy(inputs), deepcopy(outputs), **options)
        if result is None:
            result = outputs
        if not isinstance(result, list) or len(result) != len(inputs):
            raise EngineError("학습자료 항목 수가 원문과 다릅니다.")
        mapped = {item.get("id"): item for item in result if isinstance(item, dict)}
        if len(mapped) != len(inputs) or set(mapped) != {e["id"] for e in inputs}:
            raise EngineError("학습자료의 원문 연결이 올바르지 않습니다.")
        learning = {}
        for source in inputs:
            output = mapped[source["id"]]
            if any(output.get(key) != value for key, value in source.items()):
                raise EngineError("학습자료가 원문 필드를 변경했습니다.")
            if set(output) - set(source) - {"learning", "learning_stale", "learning_pending", "learning_input_fingerprint"}:
                raise EngineError("허용하지 않은 학습자료 필드입니다.")
            material = output.get("learning")
            if not isinstance(material, dict) or not material:
                raise EngineError("필수 학습자료가 없습니다.")
            learning[source["id"]] = deepcopy(material)
        return learning

    def run_batch(self, job_id, batch_id):
        claimed = self._claim(job_id, batch_id)
        if claimed is None:
            return {"state": "complete", "reused_complete_batch": True}
        job, batch, token = claimed
        cancelled = threading.Event()
        try:
            with _heartbeat_loop(self.heartbeat_seconds, lambda: self._heartbeat(job, batch_id, token),
                                 name="learning-lease", on_error=cancelled.set):
                all_inputs, spec = batch["inputs"], job["manifest"]["spec"]
                if digest(all_inputs) != batch["input_hash"]:
                    raise EngineError("학습 묶음의 원문 기준표가 변경되었습니다.")
                inputs = [e for e in all_inputs if generation_eligible(e)] if before_review(job['manifest']) else all_inputs
                learning, missing = {}, []
                for entry in inputs:
                    cached = self.store.get("cache/" + cache_key(entry, spec)) if self.reuse else None
                    if cached:
                        try:
                            rebound = {**deepcopy(entry), "learning": deepcopy(cached["learning"])}
                            if self.rebind_cache is not None:
                                rebound = self.rebind_cache(rebound)
                            learning.update(self._validated([entry], [rebound], spec))
                            continue
                        except (EngineError, ValueError, KeyError, TypeError):
                            pass
                    missing.append(entry)
                if missing:
                    options = {'spec': deepcopy(spec)} if self.versioned_provider else {}
                    outputs = self.provider(deepcopy(missing), cancel_event=cancelled, **options)
                    learning.update(self._validated(missing, outputs, spec))
                if cancelled.is_set():
                    raise LeaseLost("학습 묶음 실행 중 소유권 확인에 실패했습니다.")
                self._assert_current(job, batch)
                self._complete_batch(job, batch, token, learning)
                return {"state": "complete", "generated": len(missing), "reused": len(inputs) - len(missing)}
        except Exception as error:
            self._record_failure(job, batch, token, error)
            raise

    def _complete_batch(self, job, batch, token, learning):
        job_key = self._job(job["id"])
        batch_key, account_key = self._batch(job["id"], batch["id"]), job["account_key"]
        caches = {"cache/" + cache_key(e, job["manifest"]["spec"]): {"learning": learning[e["id"]],
                  "spec": job["manifest"]["spec"], "validated_at": self.clock()} for e in batch["inputs"] if e['id'] in learning}
        def commit(values):
            current, account = values[batch_key], values[account_key]
            self._owned(current, token)
            if values[job_key]["state"] in TERMINAL:
                raise SourceChanged("종료된 작업 결과를 저장하지 않았습니다.")
            current.update(state="complete", learning=learning, result_hash=digest(learning), claim=None,
                           completed_at=self.clock(), error_code=None)
            saved_job = values[job_key]
            saved_job["completed_entries"] += len(learning) if before_review(job['manifest']) else len(current["inputs"])
            saved_job["completed_batches"] += 1
            account["slots"] = [s for s in account["slots"] if s["token"] != token]
            return {job_key: saved_job, batch_key: current, account_key: account, **caches}, None
        self.store.atomic([job_key, batch_key, account_key, *caches], commit)

    def _record_failure(self, job, batch, token, error):
        job_key, batch_key = self._job(job["id"]), self._batch(job["id"], batch["id"])
        account_key = job["account_key"]
        # Persist fixed codes, never provider messages or source material.
        quality_hold = isinstance(error, LearningReviewRequired)
        code = error.code if quality_hold else getattr(error, "code", "source_changed") if isinstance(error, SourceChanged) else "lease_lost" if isinstance(error, LeaseLost) else "generation_failed"
        def fail(values):
            current, saved_job, account = values[batch_key], values[job_key], values[account_key]
            if (current.get("claim") or {}).get("token") != token:
                return {}, None
            terminal = quality_hold or isinstance(error, SourceChanged) or current["attempts"] >= self.max_attempts
            current.update(state='needs_review' if quality_hold else "superseded" if isinstance(error, SourceChanged) else "failed" if terminal else "pending", claim=None, error_code=code,
                           next_attempt_at=self.clock() + min(60, 2 ** min(current["attempts"], 6)),
                           dispatch_generation=current["dispatch_generation"] + 1)
            if terminal:
                saved_job.update(state='needs_review' if quality_hold else ("needs_review" if self.current_bank is not None else "superseded") if isinstance(error, SourceChanged) else "failed", error_code=code)
            if quality_hold:
                ids = {entry['id'] for entry in current['inputs']}
                current['review_entry_ids'] = [entry_id for entry_id in error.entry_ids if entry_id in ids]
                saved_job.setdefault('review_entries', {}).update({entry_id: code for entry_id in current['review_entry_ids']})
            account["slots"] = [s for s in account["slots"] if s["token"] != token]
            return {batch_key: current, job_key: saved_job, account_key: account}, None
        try:
            self.store.atomic([job_key, batch_key, account_key], fail)
        except Exception:
            # The persisted lease expires; reconciler will recover. Do not mask
            # the original failure or pretend its outcome was acknowledged.
            pass

    def finalize(self, job_id):
        if self.publication_context is None:
            return self._finalize(job_id)
        before = self.store.get(self._job(job_id))
        if not before:
            raise KeyError(job_id)
        if before["state"] == "complete":
            return self.status(job_id)
        with self.publication_context(before["manifest"]["bank"]["id"]):
            return self._finalize(job_id)

    def _finalize(self, job_id):
        key, now, token = self._job(job_id), self.clock(), uuid.uuid4().hex
        before = self.store.get(key)
        if not before:
            raise KeyError(job_id)
        if before["state"] == "complete":
            return self.status(job_id)
        self._assert_current(before)
        batch_keys = [self._batch(job_id, b) for b in before["batch_ids"]]
        def claim(values):
            job = values[key]
            if job["state"] == "complete":
                return {}, None
            if job["state"] in TERMINAL:
                raise SourceChanged("종료된 작업은 저장할 수 없습니다.")
            if (job.get("claim") or {}).get("expires_at", 0) > now:
                raise WorkBusy("학습자료를 저장 중입니다.")
            if job["completed_batches"] != len(batch_keys):
                raise WorkBusy("아직 완료되지 않은 학습 묶음이 있습니다.")
            job.update(state="publishing", claim={"token": token, "expires_at": now + self.lease_seconds},
                       publish_attempts=job.get("publish_attempts", 0) + 1)
            return {key: job}, job
        claimed = self.store.atomic([key], claim)
        if claimed is None:
            return self.status(job_id)
        job = claimed
        lost = threading.Event()
        def renew_publication(values):
            current = values[key]
            self._owned(current, token)
            current["claim"]["expires_at"] = self.clock() + self.lease_seconds
            return {key: current}, None
        try:
            with _heartbeat_loop(self.heartbeat_seconds, lambda: self.store.atomic([key], renew_publication),
                                 name="learning-publication-lease", on_error=lost.set):
                # Completed batch documents are immutable. Read large result sets
                # outside the short transaction instead of exceeding its size limit.
                batches = [self.store.get(k) for k in batch_keys]
                if any(not b or b["state"] != "complete" for b in batches):
                    raise EngineError("완료된 학습 묶음을 확인할 수 없습니다.")
                entries = []
                for batch in batches:
                    if digest(batch["inputs"]) != batch["input_hash"] or digest(batch["learning"]) != batch["result_hash"]:
                        raise EngineError("저장된 학습자료를 검증할 수 없습니다.")
                    targets = [e for e in batch['inputs'] if generation_eligible(e)] if before_review(job['manifest']) else batch['inputs']
                    if set(batch['learning']) != {e['id'] for e in targets}:
                        raise EngineError('저장된 학습자료의 생성 대상이 일치하지 않습니다.')
                    outputs = [{**deepcopy(e), **({'learning': deepcopy(batch['learning'][e['id']])}
                        if e['id'] in batch['learning'] else {})} for e in batch['inputs']]
                    self._validated(targets, [e for e in outputs if e['id'] in batch['learning']], job['manifest']['spec'])
                    entries.extend(outputs)
                manifest = deepcopy(job["manifest"])
                manifest["bank"]["entries"] = [{k: deepcopy(v) for k, v in e.items() if k != "learning"} for e in entries]
                source_rows = sorted((row for b in batches for row in b["source_rows"]), key=lambda row: row["index"])
                manifest["bank"]["source_manifest"]["entries"] = [deepcopy(row["entry"]) for row in source_rows]
                manifest["input_fingerprints"] = [input_fingerprint(e) for e in manifest["bank"]["entries"]]
                if digest({k: v for k, v in manifest.items() if k != "sha256"}) != manifest["sha256"]:
                    raise EngineError("최종 원문 기준표가 일치하지 않습니다.")
                self._assert_current(job)
                if lost.is_set():
                    raise LeaseLost("최종 저장 실행 소유권을 확인할 수 없습니다.")
                # Retain an acknowledged publication receipt before readback. If
                # that read fails, retry verification, not Drive publication or AI.
                receipt = job.get("pending_receipt")
                if receipt is None:
                    self.store.atomic([key], renew_publication)
                    receipt = self.publish(manifest, deepcopy(entries), job_id)
                    def remember(values):
                        current = values[key]
                        self._owned(current, token)
                        current["pending_receipt"] = receipt
                        return {key: current}, None
                    self.store.atomic([key], remember)
                published = self.readback(receipt)
                if generation_version(published, preparation_policy=manifest.get('preparation_policy', 'after_review_v1')) != manifest["source_version"]:
                    raise SourceChanged("최종 저장 원문이 생성 기준과 다릅니다.")
                target_ids = {e['id'] for e in entries if 'learning' in e}
                stored = {e["id"]: e.get("learning") for e in published["entries"] if e['id'] in target_ids}
                expected = {e["id"]: e["learning"] for e in entries if e['id'] in target_ids}
                if digest(stored) != digest(expected):
                    raise EngineError("최종 저장 학습자료가 검증 결과와 다릅니다.")
                self._assert_current(job)
                if lost.is_set():
                    raise LeaseLost("최종 저장 실행 소유권을 확인할 수 없습니다.")
                def done(values):
                    current = values[key]
                    self._owned(current, token)
                    current.update(state="complete", claim=None, receipt=receipt, completed_at=self.clock(),
                                   error_code=None, elapsed_seconds=self.clock() - current["created_at"])
                    return {key: current}, None
                self.store.atomic([key], done)
        except Exception as error:
            def retry(values):
                current = values[key]
                if (current.get("claim") or {}).get("token") != token:
                    return {}, None
                current.update(state=("needs_review" if self.current_bank is not None else "superseded") if isinstance(error, SourceChanged) else
                               "failed" if current.get("publish_attempts", 0) >= self.max_attempts else "publishing", claim=None,
                               error_code=getattr(error, "code", "source_changed") if isinstance(error, SourceChanged) else "publish_failed")
                return {key: current}, None
            self.store.atomic([key], retry)
            raise
        return self.status(job_id)

    def reconcile(self, dispatcher):
        """Recreate lost enqueue attempts and recover expired workers, even if UI closes.

        Invoke periodically from an authenticated scheduler. Each dispatch name
        contains a durable generation so Tasks' deduplication window is harmless.
        """
        sent, finalizers = 0, 0
        for _, job in self.store.scan("job/"):
            if job["state"] in TERMINAL:
                continue
            job_id = job["id"]
            batches = []
            for batch_id in job["batch_ids"]:
                key = self._batch(job_id, batch_id)
                job_key = self._job(job_id)
                def recover(values):
                    batch = values[key]
                    if batch["state"] == "running" and (batch.get("claim") or {}).get("expires_at", 0) <= self.clock():
                        failed = batch["attempts"] >= self.max_attempts
                        batch.update(state="failed" if failed else "pending", claim=None, dispatch_generation=batch["dispatch_generation"] + 1)
                        writes = {key: batch}
                        if failed:
                            saved_job = values[job_key]
                            saved_job.update(state="failed", error_code="worker_attempts_exhausted")
                            writes[job_key] = saved_job
                        return writes, batch
                    return {}, batch
                batch = self.store.atomic([key, job_key], recover)
                batches.append(batch)
                if batch["state"] != "pending" or batch["next_attempt_at"] > self.clock():
                    continue
                # Periodically replace a dispatched task if it exhausted its
                # retries or vanished; active claim prevents duplicate AI work.
                if batch["enqueued_generation"] == batch["dispatch_generation"] and self.clock() - batch.get("enqueued_at", 0) < self.lease_seconds * 2:
                    continue
                if batch["enqueued_generation"] == batch["dispatch_generation"]:
                    def bump(values):
                        current = values[key]
                        if current["state"] == "pending" and current["dispatch_generation"] == batch["dispatch_generation"]:
                            current["dispatch_generation"] += 1
                            return {key: current}, current
                        return {}, current
                    batch = self.store.atomic([key], bump)
                generation = batch["dispatch_generation"]
                dispatcher.enqueue({"kind": "batch", "job_id": job_id, "batch_id": batch_id, "generation": generation})
                def acknowledge(values):
                    current = values[key]
                    if current["dispatch_generation"] != generation:
                        return {}, None
                    current.update(enqueued_generation=generation, enqueued_at=self.clock())
                    return {key: current}, None
                self.store.atomic([key], acknowledge)
                sent += 1
            if job["state"] != "needs_review" and batches and all(b["state"] == "complete" for b in batches):
                # Time bucket gives a failed/previously ACKed finalizer another
                # deterministic dispatch name without ever regenerating learning.
                generation = int(self.clock() // self.lease_seconds)
                dispatcher.enqueue({"kind": "finalize", "job_id": job_id, "generation": generation})
                finalizers += 1
        return {"batches_dispatched": sent, "finalizers_dispatched": finalizers}


class CloudTasksDispatcher:
    def __init__(self, client, *, project, location, queue, worker_url, service_account, deadline_seconds=840):
        if not worker_url.startswith("https://") or not service_account.endswith(".iam.gserviceaccount.com"):
            raise ValueError("Authenticated HTTPS worker configuration required")
        if not 15 <= deadline_seconds <= 1800:
            raise ValueError("Invalid task deadline")
        self.client = client
        self.parent = client.queue_path(project, location, queue)
        self.worker_url, self.service_account = worker_url.rstrip("/"), service_account
        self.deadline_seconds = deadline_seconds

    def enqueue(self, payload):
        name = self.parent + "/tasks/learning-" + digest(payload)
        task = {"name": name, "http_request": {"http_method": 1, "url": self.worker_url + "/tasks/learning",
                "headers": {"Content-Type": "application/json"}, "body": _bytes(payload),
                "oidc_token": {"service_account_email": self.service_account, "audience": self.worker_url}},
                "dispatch_deadline": {"seconds": self.deadline_seconds}}
        try:
            self.client.create_task(request={"parent": self.parent, "task": task})
        except Exception as error:
            # AlreadyExists alone is the successful recovery of an enqueue ACK
            # loss. Permission, network, quota and all other errors propagate.
            if type(error).__name__ != "AlreadyExists":
                raise
        return name
