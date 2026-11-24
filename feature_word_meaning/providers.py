"""명시적 GPT 구독 모델 어댑터(자동 계정 전환 및 유료 외부 API 폴백 없음)."""

from __future__ import annotations

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
    """요청 객체를 strict GPT JSON 스키마로 변환한다."""
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
    """운영 앱의 ChatGPT 구독 디스패치를 재사용한다(부팅 시 네트워크·자격증명 접근 없음)."""

    def __init__(self, config_module=None, gpt_generate=None):
        self.config = config_module
        self.gpt_generate = gpt_generate

    def __call__(self, request, model_options=None):
        options = model_options or {}
        if not isinstance(options, dict) or set(options) - {"provider", "reasoning"}:
            raise ProviderUnavailable("AI 모델 선택 형식이 올바르지 않습니다.")
        if options.get("provider", "gpt") != "gpt":
            raise ProviderUnavailable("GPT-6.1 Sol 모델만 지원합니다.", category="unsupported_model")
        config = self.config or importlib.import_module("app.config")
        system = request.get("system_prompt", "")
        prompt = request.get("user_prompt", "")
        if not isinstance(system, str) or not isinstance(prompt, str) or not system or not prompt:
            raise ProviderUnavailable("생성할 출제 자료가 없습니다.")
        effort = options.get("reasoning", config.GPT_REASONING_DEFAULT)
        if effort not in config.GPT_REASONING_LEVELS:
            raise ProviderUnavailable("추론 수준은 Light, Medium, High, XHigh 중 선택하세요.")
        generate = self.gpt_generate or importlib.import_module("app.gpt_subscription").generate
        try:
            return generate(system, prompt, effort, response_json_schema(request))
        except Exception as error:
            # SubscriptionError는 공개 가능한 메시지 규약을 따른다.
            if type(error).__name__ == "SubscriptionError" and type(error).__module__ == "app.gpt_subscription":
                diagnostic = getattr(error, "diagnostic", {})
                category = diagnostic.get("category", "other") if isinstance(diagnostic, dict) else "other"
                if category == "other" and any(text in str(error) for text in ("로그인", "만료", "계정이 아직 연결", "재연결", "API 키 연결은")):
                    category = "auth"
                raise ProviderUnavailable(str(error), category=category, status=getattr(error, "status", None)) from None
            raise ProviderUnavailable(
                "GPT 구독 생성을 완료하지 못했습니다. 계정 연결과 사용 한도를 확인한 뒤 재시도하세요.",
                category="connection" if isinstance(error, (OSError, TimeoutError)) else "other",
            ) from None
