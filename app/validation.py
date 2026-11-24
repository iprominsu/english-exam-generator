"""Validate all renderable payloads before saving or opening an editor."""
from .question_context import validate_word_context
ALLOWED_TYPES = {"def_match", "sentence_match", "common_blank", "count_correct", "blank_ko", "word_meaning", "passage_blank", "pair_ko"}


def validate_questions(data, *, check_context=True, generated=False):
    if not isinstance(data, list) or not 1 <= len(data) <= 50:
        raise ValueError("문항은 1~50개의 배열이어야 합니다.")
    for i, q in enumerate(data, 1):
        prefix = f"{i}번 문항: "
        def fail(message):
            raise ValueError(prefix + message)
        def text(value, name, empty=False):
            if not isinstance(value, str) or len(value) > 15000 or (not empty and not value.strip()):
                fail(name + " 텍스트를 확인해 주세요.")
        if not isinstance(q, dict) or q.get("type") not in ALLOWED_TYPES:
            fail("지원하지 않는 문항 유형입니다.")
        text(q.get("instruction"), "지시문")
        text(q.get("explanation", ""), "해설", True)
        p = q.get("payload")
        if not isinstance(p, dict):
            fail("문항 내용은 객체여야 합니다.")
        def array(key, low, high):
            value = p.get(key)
            if not isinstance(value, list) or not low <= len(value) <= high:
                fail(key + f" 항목 수는 {low}~{high}개여야 합니다.")
            return value
        t = q["type"]
        if t in ("def_match", "pair_ko", "sentence_match"):
            key, fields = ("items", ("en", "u", "ko")) if t == "sentence_match" else ("pairs", ("word", "def" if t == "def_match" else "ko"))
            options = array(key, 5, 5)
            for row in options:
                if not isinstance(row, dict):
                    fail(key + " 항목 형식을 확인해 주세요.")
                for field in fields:
                    text(row.get(field), field)
        else:
            options = array("choices", 5, 5)
            for option in options:
                text(option, "보기")
            if t == "common_blank":
                for sentence in array("sentences", 2, 5):
                    text(sentence, "예문")
            if t == "count_correct":
                for row in array("table", 1, 30):
                    if not isinstance(row, dict):
                        fail("표 항목 형식을 확인해 주세요.")
                    text(row.get("word"), "단어")
                    text(row.get("ko"), "뜻")
            for field in (("sentence", "ko") if t == "blank_ko" else ("passage",) if t == "passage_blank" else ()):
                text(p.get(field), field)
        if type(q.get("answer")) is not int or not 1 <= q["answer"] <= len(options):
            fail("정답은 1~5 사이의 정수여야 합니다.")
        if check_context:
            try:
                validate_word_context(q, generated=generated)
            except ValueError as error:
                fail(str(error))
        q["no"] = i
    return data
