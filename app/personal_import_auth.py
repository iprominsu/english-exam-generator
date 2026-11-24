"""Private adapter for the owner's existing Codex login cache.

No OAuth endpoint or inference API is implemented here. One distributed lease
owns all managed app-server refreshes and writes to the existing credential
store. Inference processes receive only the documented external-token fields.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import threading
import time

from . import config
from .gpt_subscription import CredentialStore, ExternalAuthCoordinator, SubscriptionError, session


EXPECTED_EMAIL = os.environ.get("APPROVED_GPT_EMAIL", "").strip().lower()


def _claims(value):
    """Read metadata from the trusted private cache, never log token contents.

    This is not JWT signature verification. Unknown email metadata is resolved
    through managed account/read; the app-server validates authentication.
    """
    if not isinstance(value, str) or len(value) > 1_000_000:
        return {}
    try:
        encoded = value.split(".")[1]
        decoded = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
        return decoded if isinstance(decoded, dict) else {}
    except (IndexError, ValueError, TypeError):
        return {}


def _private_payload(raw, *, verified_account=None):
    if not EXPECTED_EMAIL:
        raise SubscriptionError("개인 도구에 승인된 GPT 계정을 설정해 주세요.")
    try:
        if len(raw) > 1_000_000:
            raise ValueError()
        auth = json.loads(raw)
        tokens = auth.get("tokens", {})
        if auth.get("auth_mode") != "chatgpt" or not isinstance(tokens, dict):
            raise ValueError()
        access = tokens.get("access_token")
        if not isinstance(access, str) or not access:
            raise ValueError()
    except (ValueError, TypeError, AttributeError):
        raise SubscriptionError("기존 개인 GPT 로그인 정보를 확인해 주세요.") from None
    identity, access_claims = _claims(tokens.get("id_token")), _claims(access)
    metadata = [claims.get("https://api.openai.com/auth", {}) for claims in (identity, access_claims)]
    metadata = [value for value in metadata if isinstance(value, dict)]
    emails = {str(claims.get("email")).strip().lower() for claims in (identity, access_claims) if claims.get("email")}
    profile = access_claims.get("https://api.openai.com/profile", {})
    if isinstance(profile, dict) and profile.get("email"):
        emails.add(str(profile["email"]).strip().lower())
    if emails and emails != {EXPECTED_EMAIL}:
        raise SubscriptionError("개인 도구에 승인된 GPT 계정이 아닙니다.")
    verified_account = verified_account or {}
    if verified_account:
        if (verified_account.get("type") != "chatgpt"
                or str(verified_account.get("email", "")).strip().lower() != EXPECTED_EMAIL):
            raise SubscriptionError("개인 도구에 승인된 GPT 계정 연결을 확인해 주세요.")
        emails.add(EXPECTED_EMAIL)
    account_ids = {str(value).strip() for value in [tokens.get("account_id"),
        *(item.get("chatgpt_account_id") for item in metadata)] if value}
    if len(account_ids) > 1:
        raise SubscriptionError("기존 GPT 로그인 계정 식별자가 일치하지 않습니다.")
    payload = {"accessToken": access, "chatgptAccountId": next(iter(account_ids), "")}
    plan = next((item["chatgpt_plan_type"] for item in metadata if item.get("chatgpt_plan_type")), verified_account.get("planType"))
    if isinstance(plan, str) and plan:
        payload["chatgptPlanType"] = plan
    expiration = access_claims.get("exp")
    if isinstance(expiration, bool) or not isinstance(expiration, (int, float)):
        expiration = None
    return payload, bool(emails), expiration


def create_coordinator(*, state_store, credential_store_factory=None, session_factory=None,
                       temp_root=None, clock=time.time):
    """Construction is lazy: no credentials, subprocess, or remote calls.

    Explicit personal-use confirmation is independent of commercial hosted
    token authorization. Production and QA share this credential lease even
    when their job collections differ.
    """
    if (not getattr(config, "IMPORT_ENGINE_ENABLED", False)
            or not getattr(config, "IMPORT_PERSONAL_USE_CONFIRMED", False)):
        raise SubscriptionError("개인 도구 사용 확인과 새 생성 엔진 설정이 필요합니다.")
    if not EXPECTED_EMAIL:
        raise SubscriptionError("개인 도구에 승인된 GPT 계정을 설정해 주세요.")
    if not config.CODEX_AUTH_SECRET:
        raise SubscriptionError("기존 개인 GPT 연결 저장소를 확인해 주세요.")
    if state_store is None:
        raise SubscriptionError("공유 인증 갱신 잠금이 필요합니다.")
    from feature_word_meaning.import_engine import distributed_lease, digest
    key = "lease/auth-refresh/" + digest(config.CODEX_AUTH_SECRET)
    credential_store_factory = credential_store_factory or CredentialStore
    session_factory = session_factory or session
    temp_root = Path(temp_root) if temp_root is not None else Path(__file__).resolve().parents[1] / ".local" / "personal-auth"
    active = threading.local()

    @contextmanager
    def lease_factory():
        with distributed_lease(state_store, key, clock=clock) as lease:
            active.lease = lease
            try:
                yield lease
            finally:
                active.lease = None

    def assert_owned():
        lease = getattr(active, "lease", None)
        if lease is None:
            raise SubscriptionError("공유 인증 갱신 소유권을 확인할 수 없습니다.")
        lease.assert_owned()

    def managed_refresh(*, refresh, previous_id=None):
        assert_owned()
        store, verified = credential_store_factory(), {}
        def validate_updated(raw):
            payload, known_email, _ = _private_payload(raw, verified_account=verified)
            if not known_email or not payload["chatgptAccountId"] or (previous_id and payload["chatgptAccountId"] != previous_id):
                raise SubscriptionError("갱신한 개인 GPT 계정을 확인할 수 없습니다.")
        with session_factory(store=store, temp_root=temp_root, before_persist=assert_owned,
                             validate_auth=validate_updated, expected_email=EXPECTED_EMAIL) as client:
            account = client.rpc("account/read", {"refreshToken": bool(refresh)}).get("account") or {}
            # Validate before committing refreshed credentials on context exit.
            verified.update(account or {"type": "unknown"})
            if account.get("type") != "chatgpt" or str(account.get("email", "")).strip().lower() != EXPECTED_EMAIL:
                raise SubscriptionError("개인 도구에 승인된 GPT 계정 연결을 확인해 주세요.")
        assert_owned()
        raw = credential_store_factory().load()
        validate_updated(raw)
        payload, _, _ = _private_payload(raw, verified_account=verified)
        return payload

    def load():
        assert_owned()
        payload, known_email, expiration = _private_payload(credential_store_factory().load())
        if not known_email or not payload["chatgptAccountId"]:
            return managed_refresh(refresh=False, previous_id=payload["chatgptAccountId"] or None)
        if expiration is not None and expiration <= clock() + 120:
            return managed_refresh(refresh=True, previous_id=payload["chatgptAccountId"])
        return payload

    def refresh():
        # ExternalAuthCoordinator reloads inside the same lease before calling
        # this function, deduplicating refresh requests from separate workers.
        payload, _, _ = _private_payload(credential_store_factory().load())
        return managed_refresh(refresh=True, previous_id=payload["chatgptAccountId"] or None)

    return ExternalAuthCoordinator(load, refresh, lease_factory=lease_factory)
