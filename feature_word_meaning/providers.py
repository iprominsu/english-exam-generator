"""Explicit model adapters. No automatic change of account or paid provider."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
import importlib


class ProviderUnavailable(RuntimeError):
    def __init__(self, message, *, category="other", status=None):
        super().__init__(message)
        allowed = {"other", "connection", "auth", "rate_limit", "model_not_found", "unsupported_model", "unsupported_reasoning", "invalid_schema"}
        self.category = category if category in allowed else "other"
        self.status = status if isinstance(status, int) else None


def _object_schema(properties):
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


def response_json_schema(request):
    """Turn the exam/review example into the actual strict GPT JSON schema."""
    if "bank_import_schema" in request:
        return request["bank_import_schema"]
    string = {"type": "string", "minLength": 1}
    integer = {"type": "integer", "minimum": 0}
    def array(items, minimum=1):
        return {"type": "array", "items": items, "minItems": minimum}
    example = request.get("response_schema", {})
    if "questions" in example:
        span = _object_schema({"entry_id": string, "start": integer, "end": {"type": "integer", "minimum": 1}, "quote": string})
        group = _object_schema({"source_spans": array(span)})
        question = _object_schema({"word_id": string, "groups": array(group), "distractors": array(_object_schema({"text": string}), 2)})
        return _object_schema({"questions": array(question)})
    if "reviews" in example:
        boolean = {"type": "boolean"}
        distractor = _object_schema({"choice_id": string, "is_valid_meaning": boolean, "overlaps_correct": boolean, "reason": string})
        review = _object_schema({"question_id": string, "content_hash": string, "source_coverage_complete": boolean, "grouping_is_accurate": boolean, "correct_choices_use_only_source": boolean, "distractors": array(distractor, 2), "reason": string})
        return _object_schema({"reviews": array(review)})
    raise ProviderUnavailable("지원하지 않는 생성 응답 형식입니다.")


class OperatingProvider:
    """Reuse the operating app's subscription/Gemini dispatch, with no fallback.

    Dependencies are loaded only on the first model call, never at app boot.
    Injectable functions support offline tests without credentials or accounts.
    """
    def __init__(self, config_module=None, gpt_generate=None, gemini_generate=None):
        self.config = config_module
        self.gpt_generate = gpt_generate
        self.gemini_generate = gemini_generate

    def __call__(self, request, model_options=None):
        options = model_options or {}
        if not isinstance(options, dict) or set(options) - {"provider", "reasoning"}:
            raise ProviderUnavailable("AI 모델 선택 형식이 올바르지 않습니다.")
        provider = options.get("provider", "gpt")
        if provider not in {"gpt", "gemini"}:
            raise ProviderUnavailable("GPT-6.1 Sol 또는 Gemini 중 선택하세요.")
        config = self.config or importlib.import_module("app.config")
        system = request.get("system_prompt", "")
        prompt = request.get("user_prompt", "")
        if not isinstance(system, str) or not isinstance(prompt, str) or not system or not prompt:
            raise ProviderUnavailable("생성할 출제 자료가 없습니다.")
        if provider == "gpt":
            effort = options.get("reasoning", config.GPT_REASONING_DEFAULT)
            if effort not in config.GPT_REASONING_LEVELS:
                raise ProviderUnavailable("추론 수준은 Light, Medium, High, XHigh 중 선택하세요.")
            generate = self.gpt_generate or importlib.import_module("app.gpt_subscription").generate
            try:
                return generate(system, prompt, effort, response_json_schema(request))
            except Exception as error:
                # SubscriptionError is explicitly designed as a public message.
                if type(error).__name__ == "SubscriptionError" and type(error).__module__ == "app.gpt_subscription":
                    diagnostic = getattr(error, "diagnostic", {})
                    category = diagnostic.get("category", "other") if isinstance(diagnostic, dict) else "other"
                    if category == "other" and any(text in str(error) for text in ("로그인", "만료", "계정이 아직 연결", "재연결", "API 키 연결은")):
                        category = "auth"
                    raise ProviderUnavailable(str(error), category=category, status=getattr(error, "status", None)) from None
                raise ProviderUnavailable("GPT 구독 생성을 완료하지 못했습니다. 계정 연결과 사용 한도를 확인한 뒤 재시도하세요.",
                                          category="connection" if isinstance(error, (OSError, TimeoutError)) else "other") from None
        generate = self.gemini_generate or importlib.import_module("app.llm")._gemini_generate
        try:
            return generate(system, prompt)
        except Exception:
            raise ProviderUnavailable("Gemini 생성을 완료하지 못했습니다. 선택한 Gemini 설정과 한도를 확인한 뒤 재시도하세요.") from None


class CallableProvider:
    """Wrap the operating app's existing GPT subscription / Gemini dispatcher."""

    def __init__(self, callback):
        self.callback = callback

    def __call__(self, request, model_options=None):
        return self.callback(request, model_options or {})


class GeminiProvider:
    """Use only when Gemini is explicitly selected and configured by the owner."""

    def __init__(self, api_key, model):
        if not api_key or not re.fullmatch(r"[a-zA-Z0-9_.-]+", model or ""):
            raise ProviderUnavailable("Gemini 키와 사용할 모델 설정이 필요합니다.")
        self.api_key, self.model = api_key, model

    def __call__(self, request, model_options=None):
        payload = {
            "systemInstruction": {"parts": [{"text": request.get("system_prompt", "")}]},
            "contents": [{"role": "user", "parts": [{"text": request["user_prompt"]}]}],
            "generationConfig": {"responseMimeType": "application/json"},
        }
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), method="POST",
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as response:
                data = json.load(response)
        except urllib.error.HTTPError as error:
            # Do not persist response bodies or credential-bearing URLs in jobs/logs.
            raise ProviderUnavailable(f"Gemini 요청 실패(HTTP {error.code}). 모델·한도·계정 설정을 확인해 주세요.") from None
        except (urllib.error.URLError, TimeoutError):
            raise ProviderUnavailable("Gemini 연결이 실패했습니다. 저장된 작업에서 재시도해 주세요.") from None
        candidates = data.get("candidates", [])
        text = "".join(part.get("text", "") for part in candidates[0].get("content", {}).get("parts", [])) if candidates else ""
        try:
            return json.loads(text)
        except (ValueError, TypeError):
            raise ProviderUnavailable("모델이 유효한 JSON 결과를 반환하지 않았습니다.") from None
