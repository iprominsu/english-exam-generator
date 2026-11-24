"""Opt-in cloud composition. Disabled application boot performs no cloud I/O.

Either personal ownership of the existing Codex login or hosted token ownership
must be explicitly confirmed. Both use an application-owned coordinator;
personal cache refreshes share a durable lease across production and QA.
"""
from __future__ import annotations

from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass
import importlib
import os
from pathlib import Path
import re
import threading


class ImportConfigurationError(RuntimeError):
    pass


def _bool(value):
    return str(value).strip().lower() in {"1", "true", "yes"}


@dataclass(frozen=True)
class CloudSettings:
    enabled: bool = False
    hosted_auth_confirmed: bool = False
    personal_use_confirmed: bool = False
    auth_factory: str = ""
    state_namespace: str = "production"
    cache_reuse: bool = True
    project: str = ""
    region: str = "us-central1"
    database: str = "melody-import"
    queue: str = "melody-learning"
    worker_url: str = ""
    task_service_account: str = ""
    scheduler_service_account: str = ""
    account_key: str = "melody-gpt-subscription"
    approved_project: str = ""
    approved_region: str = "us-central1"

    @classmethod
    def from_environment(cls, env=None):
        env = os.environ if env is None else env
        return cls(enabled=_bool(env.get("IMPORT_ENGINE_ENABLED", "false")),
            hosted_auth_confirmed=_bool(env.get("IMPORT_HOSTED_AUTH_CONFIRMED", "false")),
            personal_use_confirmed=_bool(env.get("IMPORT_PERSONAL_USE_CONFIRMED", "false")),
            auth_factory=env.get("IMPORT_AUTH_COORDINATOR_FACTORY", ""),
            state_namespace=env.get("IMPORT_STATE_NAMESPACE", "production"),
            cache_reuse=_bool(env.get("IMPORT_CACHE_REUSE", "true")),
            project=env.get("IMPORT_GCP_PROJECT", cls.project),
            region=env.get("IMPORT_GCP_REGION", cls.region),
            database=env.get("IMPORT_FIRESTORE_DATABASE", cls.database),
            queue=env.get("IMPORT_TASK_QUEUE", cls.queue),
            worker_url=env.get("IMPORT_WORKER_URL", "").rstrip("/"),
            task_service_account=env.get("IMPORT_TASK_SERVICE_ACCOUNT", ""),
            scheduler_service_account=env.get("IMPORT_SCHEDULER_SERVICE_ACCOUNT", ""),
            account_key=env.get("IMPORT_ACCOUNT_KEY", cls.account_key),
            approved_project=env.get("IMPORT_APPROVED_GCP_PROJECT", "").strip(),
            approved_region=env.get("IMPORT_APPROVED_GCP_REGION", cls.approved_region).strip())

    def validate(self):
        if not self.enabled or not (self.hosted_auth_confirmed or self.personal_use_confirmed):
            raise ImportConfigurationError("새 생성 엔진과 개인 사용 또는 호스트 인증 확인이 필요합니다.")
        if len(self.state_namespace) > 64 or not re.fullmatch(r"production|(?:qa|benchmark)-[a-z0-9]+(?:-[a-z0-9]+)*", self.state_namespace):
            raise ImportConfigurationError("운영 또는 검증용 작업 저장 범위를 확인해 주세요.")
        if (not self.approved_project or self.project != self.approved_project
                or not self.approved_region or self.region != self.approved_region):
            raise ImportConfigurationError("승인된 GCP 프로젝트와 리전을 확인해 주세요.")
        if self.database != "melody-import" or not re.fullmatch(r"[a-z][a-z0-9-]{0,99}", self.queue):
            raise ImportConfigurationError("학습 작업 저장소와 큐 설정을 확인해 주세요.")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::443)?", self.worker_url):
            raise ImportConfigurationError("인증된 HTTPS 작업자 주소가 필요합니다.")
        suffix = "@" + self.project + ".iam.gserviceaccount.com"
        if not self.task_service_account.endswith(suffix) or (self.scheduler_service_account and not self.scheduler_service_account.endswith(suffix)):
            raise ImportConfigurationError("같은 GCP 프로젝트의 작업자 서비스 계정이 필요합니다.")
        if not self.account_key:
            raise ImportConfigurationError("운영과 QA의 공통 생성 계정 키가 필요합니다.")


def _validate_auth_factory(settings):
    # Configuration is administrator-owned, never supplied in an HTTP request.
    # Restrict the import to application modules; no script paths or eval.
    if not re.fullmatch(r"app(?:\.[A-Za-z_][A-Za-z0-9_]*)+:[A-Za-z_][A-Za-z0-9_]*", settings.auth_factory):
        raise ImportConfigurationError("호스트가 관리하는 인증 coordinator factory를 명시해 주세요.")
    personal_factory = "app.personal_import_auth:create_coordinator"
    if settings.auth_factory == personal_factory and not settings.personal_use_confirmed:
        raise ImportConfigurationError("기존 개인 GPT 연결의 사용 확인이 필요합니다.")
    if settings.personal_use_confirmed and not settings.hosted_auth_confirmed and settings.auth_factory != personal_factory:
        raise ImportConfigurationError("개인 도구의 기존 GPT 연결 factory를 명시해 주세요.")


def _load_host_auth(settings, *, state_store=None):
    from app.gpt_subscription import ExternalAuthCoordinator
    _validate_auth_factory(settings)
    module, name = settings.auth_factory.split(":", 1)
    factory = getattr(importlib.import_module(module), name)
    if not callable(factory):
        raise ImportConfigurationError("호스트 인증 factory가 올바르지 않습니다.")
    auth = factory(state_store=state_store) if settings.auth_factory == "app.personal_import_auth:create_coordinator" else factory()
    if not isinstance(auth, ExternalAuthCoordinator):
        raise ImportConfigurationError("호스트가 관리하는 단일 인증 갱신 경로가 필요합니다.")
    return auth


def install_bank_write_guard(service, store):
    from fastapi import HTTPException
    from .import_engine import distributed_lease, digest, WorkBusy
    local = threading.local()
    @contextmanager
    def guard(bank_id):
        key = "lease/bank-write/" + digest(bank_id)
        previous = getattr(local, "lease", None)
        if previous is not None and previous.key == key:
            try:
                previous.assert_owned()
                yield previous
            except WorkBusy:
                raise HTTPException(409, "단어장 저장 소유권을 확인할 수 없습니다. 다시 시도해 주세요.") from None
            return
        try:
            with distributed_lease(store, key) as lease:
                local.lease = lease
                try:
                    yield lease
                finally:
                    local.lease = previous
        except WorkBusy:
            raise HTTPException(409, "같은 단어장의 저장 상태가 바뀌었습니다. 최신 내용을 확인한 뒤 다시 저장해 주세요.") from None
    def assert_owned():
        lease = getattr(local, "lease", None)
        if lease is not None:
            try:
                lease.assert_owned()
            except WorkBusy:
                raise HTTPException(409, "단어장 저장 소유권을 확인할 수 없습니다. 다시 시도해 주세요.") from None
    service.bank_write_guard = guard
    service.assert_bank_write_ownership = assert_owned


def build_runtime(service, auth=None, *, settings=None, firestore_client=None, tasks_client=None,
                  pool_factory=None, state_store=None):
    settings = settings or CloudSettings.from_environment()
    settings.validate()  # Must precede credentials, imports, clients and files.
    from app.gpt_subscription import ExternalAuthCoordinator, create_learning_pool
    if auth is None:
        _validate_auth_factory(settings)
        if settings.auth_factory != "app.personal_import_auth:create_coordinator":
            auth = _load_host_auth(settings)
    elif not isinstance(auth, ExternalAuthCoordinator):
        raise ImportConfigurationError("호스트가 관리하는 단일 인증 갱신 경로가 필요합니다.")
    from .import_engine import CloudTasksDispatcher, FirestoreStateStore, account_generation_slot
    from .import_pipeline import DurableImportCoordinator
    from .providers import response_json_schema
    pool_factory = pool_factory or create_learning_pool
    if state_store is None:
        if firestore_client is None:
            from google.cloud import firestore
            firestore_client = firestore.Client(project=settings.project, database=settings.database)
        state_store = FirestoreStateStore(firestore_client, collection="melody_learning_" + settings.state_namespace)
    if auth is None:
        auth = _load_host_auth(settings, state_store=state_store)
    # A pool allocates no process/token until a task requests inference. Its
    # additional config gate must agree with the environment on real activation.
    pool = pool_factory(auth, temp_root=Path(__file__).resolve().parents[1] / ".local" / "learning-sessions", size=2)
    try:
        if tasks_client is None:
            from google.cloud import tasks_v2
            tasks_client = tasks_v2.CloudTasksClient()
        dispatcher = CloudTasksDispatcher(tasks_client, project=settings.project, location=settings.region,
            queue=settings.queue, worker_url=settings.worker_url, service_account=settings.task_service_account)
        def provider(request, options=None, *, cancel_event=None):
            options = options or {}
            if options.get("provider", "gpt") != "gpt" or options.get("reasoning", "high") != "high":
                raise ImportConfigurationError("학습 생성은 GPT-6.1 Sol / High를 사용합니다.")
            return pool.generate(request["system_prompt"], request["user_prompt"], "high",
                                 response_json_schema(request), cancel_event=cancel_event)
        coordinator = DurableImportCoordinator(service, state_store, dispatcher,
            structured_provider=provider, account_key=settings.account_key, reuse=settings.cache_reuse)
        coordinator.session_pool = pool
        service.import_admission_guard = coordinator.assert_new_import_allowed
        install_bank_write_guard(service, state_store)
        if hasattr(service, 'backend'):
            service.backend.assert_bank_write_ownership = service.assert_bank_write_ownership
        def legacy_import_provider(request, options=None):
            # Historical jobs keep their extraction/checkpoint contract while
            # using the approved host session and the same account-wide slots.
            with account_generation_slot(state_store, settings.account_key, 'legacy_import') as cancelled:
                return provider(request, options, cancel_event=cancelled)
        service.import_provider = legacy_import_provider
        coordinator.close = pool.close
        return coordinator
    except Exception:
        pool.close()
        raise


def configure_production_imports(service, **kwargs):
    settings = kwargs.pop("settings", None) or CloudSettings.from_environment()
    if not settings.enabled:
        return None
    settings.validate()
    existing = getattr(service, "import_coordinator", None)
    if existing is not None:
        return existing
    coordinator = build_runtime(service, settings=settings, **kwargs)
    service.import_coordinator = coordinator
    return coordinator


def create_cloud_worker(service=None, *, settings=None, verify_identity=None, **kwargs):
    settings = settings or CloudSettings.from_environment()
    settings.validate()
    # The no-argument factory is suitable for uvicorn --factory. Check explicit
    # auth configuration before allocating the worker's local Drive cache.
    if kwargs.get("auth") is None:
        _validate_auth_factory(settings)
    if service is None:
        from .production import create_production_service
        service = create_production_service(Path(__file__).resolve().parents[1] / ".local" / "import-worker" / "drive-cache")
    coordinator = configure_production_imports(service, settings=settings, **kwargs)
    from .import_worker import create_worker, google_identity_verifier
    if verify_identity is None:
        allowed = {settings.task_service_account, settings.scheduler_service_account} - {""}
        verify_identity = google_identity_verifier(audience=settings.worker_url, allowed_service_accounts=allowed)
    class PublicJobWorker:
        def run_batch(self, job_id, batch_id):
            return coordinator.process("batch", job_id, batch_id)
        def finalize(self, job_id):
            return coordinator.process("finalize", job_id)
        def status(self, job_id):
            return coordinator.engine.status(job_id)
    app = create_worker(PublicJobWorker(), coordinator.dispatcher, verify_identity=verify_identity,
                        prepare_handler=coordinator.prepare, reconcile_handler=coordinator.reconcile)
    app.state.import_coordinator = coordinator
    @asynccontextmanager
    async def lifespan(application):
        try:
            yield
        finally:
            coordinator.close()
    app.router.lifespan_context = lifespan
    return app
