"""Script-only meaning-writing worksheets backed by frozen registered meanings.

Answers are copied from the selected registered rows. Display edits never rewrite
source evidence, and replacing a word samples only the worksheet's frozen scope.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any

from .exams import ExamError, _canonical, _digest, _list, _nonempty, _now, _rng, select_words

WORKSHEET_KIND = "meaning_writing_test"
SCHEMA_VERSION = 1
SOURCE_FIELDS = ("word", "source_meanings", "source_entry_ids", "source_entries", "unit_ids", "meaning_raw", "original_meaning_raw")
REVIEWED_STATUSES = frozenset({"source_verified", "human_reviewed"})


def writing_question_content_hash(question: dict) -> str:
    return _digest({key: question.get(key) for key in (
        "id", "worksheet_kind", "word", "source_word", "source_word_id", "original_word",
        "answer_text", "original_answer_text", "word_edited", "answer_edited", "unit_ids",
        "source_meanings", "source_entry_ids", "meaning_raw", "original_meaning_raw",
    )})


def _combined(source: list[dict], original: bool = False) -> str:
    return "\n".join(dict.fromkeys(item.get("original_meaning_raw", item["meaning_raw"]) if original else item["meaning_raw"] for item in source))


def validate_writing_question(question: dict, require_reviewed: bool = False) -> None:
    if not isinstance(question, dict) or question.get("worksheet_kind") != WORKSHEET_KIND:
        raise ExamError("뜻 쓰기 문항 형식이 올바르지 않습니다.", "invalid_question")
    for key, label in (("id", "문항 ID"), ("word", "영단어"), ("source_word", "출처 영단어"),
                       ("source_word_id", "출제 단어 ID"), ("answer_text", "정답 뜻"), ("original_answer_text", "등록된 정답 뜻")):
        _nonempty(question.get(key), label)
    if len(question["word"]) > 300:
        raise ExamError("영단어는 300자 이내로 입력하세요.", "invalid_question")
    if question["source_word_id"] != "word_" + _digest(_canonical(question["source_word"]))[:20]:
        raise ExamError("출처 단어 ID가 원본 영단어와 다릅니다.", "invalid_question")
    sources = _list(question.get("source_meanings"), "원본 뜻")
    if not sources:
        raise ExamError("등록된 정답 뜻이 없습니다.", "invalid_question")
    for item in sources:
        if not isinstance(item, dict):
            raise ExamError("원본 뜻 형식이 올바르지 않습니다.", "invalid_question")
        _nonempty(item.get("entry_id"), "원본 항목 ID")
        _nonempty(item.get("meaning_raw"), "원본 뜻")
        if "original_meaning_raw" in item and not isinstance(item["original_meaning_raw"], str):
            raise ExamError("최초 원문 뜻 형식이 올바르지 않습니다.", "invalid_question")
    ids = [item["entry_id"] for item in sources]
    if len(ids) != len(set(ids)) or question.get("source_entry_ids") != ids:
        raise ExamError("문항 출처가 원본 뜻과 다릅니다.", "invalid_question")
    original_answer = _combined(sources)
    if question["original_answer_text"] != original_answer or question.get("meaning_raw") != original_answer:
        raise ExamError("등록된 정답 뜻이 원본과 다릅니다.", "invalid_question")
    if question.get("original_meaning_raw") != _combined(sources, original=True):
        raise ExamError("최초 원문 뜻이 출처와 다릅니다.", "invalid_question")
    if question.get("original_word") != question["source_word"]:
        raise ExamError("최초 영단어가 출처와 다릅니다.", "invalid_question")
    if question.get("word_edited") is not (question["word"] != question["source_word"]):
        raise ExamError("영단어 수정 상태가 올바르지 않습니다.", "invalid_question")
    if question.get("answer_edited") is not (question["answer_text"] != original_answer):
        raise ExamError("정답 수정 상태가 올바르지 않습니다.", "invalid_question")
    units = _list(question.get("unit_ids"), "출처 단원")
    if not units or any(not isinstance(unit, str) or not unit for unit in units) or len(units) != len(set(units)):
        raise ExamError("문항의 출처 단원이 올바르지 않습니다.", "invalid_question")
    snapshot = question.get("source_snapshot")
    if not isinstance(snapshot, dict) or snapshot.get("id") != question["source_word_id"]:
        raise ExamError("문항 원본 스냅샷이 없습니다.", "invalid_question")
    expected = {"word": question["source_word"], "source_meanings": sources, "source_entry_ids": ids,
                "unit_ids": units, "meaning_raw": original_answer, "original_meaning_raw": question["original_meaning_raw"]}
    if any(snapshot.get(key) != value for key, value in expected.items()):
        raise ExamError("문항 원본 스냅샷이 출처와 다릅니다.", "invalid_question")
    entries = _list(snapshot.get("source_entries"), "원본 항목")
    if [entry.get("id") for entry in entries if isinstance(entry, dict)] != ids or len(entries) != len(ids):
        raise ExamError("문항 원본 항목이 누락되거나 중복되었습니다.", "invalid_question")
    for entry, source in zip(entries, sources):
        if entry.get("status") != "ready" or entry.get("unit_id") not in units or _canonical(entry.get("word", "")) != question["source_word"] or entry.get("meaning", entry.get("meaning_raw")) != source["meaning_raw"] or entry.get("meaning_raw", entry.get("meaning")) != source.get("original_meaning_raw", source["meaning_raw"]):
            raise ExamError("문항이 등록된 원본 항목과 다릅니다.", "invalid_question")
    if question.get("generation_method") != "script":
        raise ExamError("뜻 쓰기 문항은 스크립트로 생성해야 합니다.", "invalid_question")
    if any(key in question for key in ("choices", "correct_choice_ids", "semantic_groups", "candidates")):
        raise ExamError("뜻 쓰기 문항에는 객관식 보기를 저장하지 않습니다.", "invalid_question")
    status = question.get("review_status")
    if not isinstance(status, str) or status not in REVIEWED_STATUSES | {"needs_review"}:
        raise ExamError("문항 검토 상태가 올바르지 않습니다.", "invalid_question")
    if status == "source_verified" and (question["word_edited"] or question["answer_edited"]):
        raise ExamError("수정된 답안을 원문 확인 상태로 표시할 수 없습니다.", "invalid_question")
    if status == "human_reviewed":
        evidence = question.get("review_evidence")
        if not isinstance(evidence, dict) or evidence.get("content_hash") != writing_question_content_hash(question):
            raise ExamError("검토 후 문항이 바뀌었습니다. 다시 확인하세요.", "review_required")
    if require_reviewed and status not in REVIEWED_STATUSES:
        raise ExamError("수정한 단어와 정답 뜻을 확인하세요.", "review_required", question_id=question["id"])


def generate_writing_questions(words: list[dict], bank: dict | None = None, seed: Any = None) -> list[dict]:
    """Copy every registered meaning; selected word order is already final."""
    from .exams import _validate_words
    _validate_words(words)
    _rng(seed)
    labels = {unit["id"]: unit["label"] for unit in (bank or {}).get("units", [])}
    result = []
    for word in words:
        source = deepcopy(word["source_meanings"])
        answer = _combined(source)
        source_word = word["word"]
        question = {
            "id": "question_" + _digest({"kind": WORKSHEET_KIND, "word": word["id"], "source": source})[:20],
            "worksheet_kind": WORKSHEET_KIND, "source_word_id": word["id"], "word": source_word,
            "source_word": source_word, "original_word": source_word, "word_edited": False,
            "answer_text": answer, "original_answer_text": answer, "answer_edited": False,
            "meaning_raw": answer, "original_meaning_raw": _combined(source, original=True),
            "source_meanings": source, "source_entry_ids": list(word["source_entry_ids"]),
            "unit_ids": list(word["unit_ids"]), "source_snapshot": deepcopy(word),
            "source_unit_labels": [labels[unit] for unit in word["unit_ids"] if unit in labels],
            "review_status": "source_verified", "review_evidence": None, "generation_method": "script",
        }
        validate_writing_question(question)
        result.append(question)
    return result


def edit_writing_question(question: dict, word: str | None = None, answer_text: str | None = None) -> dict:
    validate_writing_question(question)
    result = deepcopy(question)
    if word is not None:
        result["word"] = _nonempty(word, "영단어")
    if answer_text is not None:
        result["answer_text"] = _nonempty(answer_text, "정답 뜻")
    changed = result["word"] != question["word"] or result["answer_text"] != question["answer_text"]
    result["word_edited"] = result["word"] != result["source_word"]
    result["answer_edited"] = result["answer_text"] != result["original_answer_text"]
    if changed:
        result["review_status"] = "needs_review"
        result["review_evidence"] = None
    validate_writing_question(result)
    return result


def create_writing_worksheet(bank: dict, unit_ids: list[str], questions: list[dict], title: str | None = None,
                             selected_words: list[dict] | None = None, allow_partial: bool = False) -> dict:
    scoped = select_words(bank, unit_ids, allow_partial=allow_partial)
    by_id = {word["id"]: word for word in scoped}
    words = selected_words if selected_words is not None else scoped
    from .exams import _validate_words
    _validate_words(words)
    for word in words:
        canonical = by_id.get(word["id"])
        if canonical is None or any(canonical.get(key) != word.get(key) for key in SOURCE_FIELDS):
            raise ExamError("선택된 단어가 현재 단원 범위와 다릅니다.", "invalid_selection")
    questions = _list(questions, "시험지 문항")
    for question in questions:
        validate_writing_question(question)
    if Counter(question["source_word_id"] for question in questions) != Counter(word["id"] for word in words):
        raise ExamError("선택한 단어의 문항이 빠지거나 중복되었습니다.", "incomplete_generation")
    by_question = {question["source_word_id"]: question for question in questions}
    ordered = []
    for word in words:
        question = by_question[word["id"]]
        if question["source_snapshot"] != word:
            raise ExamError("문항 원본이 선택한 단어와 다릅니다.", "invalid_question")
        ordered.append(deepcopy(question))
    worksheet = {
        "id": "worksheet_" + _digest({"time": _now(), "questions": ordered})[:24],
        "schema_version": SCHEMA_VERSION, "worksheet_kind": WORKSHEET_KIND,
        "title": _nonempty(title if title is not None else bank.get("title", bank.get("source", {}).get("filename", "단어장")) + " 뜻 쓰기 시험", "시험지 제목"),
        "instruction": "주어진 어휘의 뜻을 쓰시오.", "created_at": _now(),
        "selected_unit_ids": list(unit_ids), "selected_units": deepcopy([unit for unit in bank["units"] if unit["id"] in set(unit_ids)]),
        "source": deepcopy(bank.get("source", {})), "source_entries": [deepcopy(entry) for word in words for entry in word["source_entries"]],
        "questions": ordered, "partial_selection": allow_partial, "generation_method": "script", "generation_version": 1,
        "generation_bank_snapshot": deepcopy(bank),
        "selection_counts": {"ready_original_entries": sum(entry.get("status") == "ready" and entry.get("unit_id") in set(unit_ids) for entry in bank["entries"]),
                             "review_original_entries": sum(entry.get("status") == "review" and entry.get("unit_id") in set(unit_ids) for entry in bank["entries"]),
                             "available_words": len(scoped), "selected_words": len(words)},
    }
    if bank.get("id"):
        worksheet["bank_id"] = bank["id"]
    validate_writing_worksheet(worksheet, require_reviewed=False)
    return worksheet


def validate_writing_worksheet(worksheet: dict, require_reviewed: bool = True) -> None:
    if not isinstance(worksheet, dict) or worksheet.get("worksheet_kind") != WORKSHEET_KIND or worksheet.get("schema_version") != SCHEMA_VERSION:
        raise ExamError("지원하지 않는 뜻 쓰기 시험지 형식입니다.", "invalid_worksheet")
    _nonempty(worksheet.get("id"), "시험지 ID")
    _nonempty(worksheet.get("title"), "시험지 제목")
    questions = _list(worksheet.get("questions"), "시험지 문항")
    if not questions:
        raise ExamError("시험지 문항이 없습니다.", "invalid_worksheet")
    for question in questions:
        validate_writing_question(question, require_reviewed=require_reviewed)
    for key in ("id", "source_word_id"):
        if len({question[key] for question in questions}) != len(questions):
            raise ExamError("시험지에 중복 문항이 있습니다.", "invalid_worksheet")
    if len({_canonical(question["word"]) for question in questions}) != len(questions):
        raise ExamError("시험지에 같은 영단어가 중복되었습니다.", "invalid_worksheet")
    units = _list(worksheet.get("selected_unit_ids"), "선택 단원")
    if not units or any(not isinstance(unit, str) or not unit for unit in units) or len(units) != len(set(units)):
        raise ExamError("시험지의 단원 범위가 올바르지 않습니다.", "invalid_worksheet")
    entries = _list(worksheet.get("source_entries"), "출처 스냅샷")
    if any(not isinstance(entry, dict) or not isinstance(entry.get("id"), str) for entry in entries):
        raise ExamError("출처 스냅샷 형식이 올바르지 않습니다.", "invalid_worksheet")
    entries_by_id = {entry["id"]: entry for entry in entries}
    if len(entries_by_id) != len(entries):
        raise ExamError("출처 스냅샷 ID가 중복되었습니다.", "invalid_worksheet")
    used = []
    for question in questions:
        if not set(question["unit_ids"]).issubset(units):
            raise ExamError("문항의 출처가 선택 범위를 벗어났습니다.", "invalid_worksheet")
        for entry in question["source_snapshot"]["source_entries"]:
            if entry["unit_id"] not in units or entries_by_id.get(entry["id"]) != entry:
                raise ExamError("문항이 저장된 원본 출처와 다릅니다.", "invalid_worksheet")
            used.append(entry["id"])
    if Counter(used) != Counter(entries_by_id.keys()):
        raise ExamError("시험지 원본 항목이 중복되거나 누락되었습니다.", "invalid_worksheet")


def replace_writing_question(worksheet: dict, question_id: str, bank: dict | None = None, seed: Any = None) -> dict:
    """Return a new snapshot with one unused scoped word; never mutate inputs."""
    validate_writing_worksheet(worksheet, require_reviewed=False)
    old = next((question for question in worksheet["questions"] if question["id"] == question_id), None)
    if old is None:
        raise ExamError("문항을 찾을 수 없습니다.", "question_not_found")
    frozen_bank = worksheet.get("generation_bank_snapshot") or bank
    if not isinstance(frozen_bank, dict):
        raise ExamError("다른 단어 출제에 필요한 원본 스냅샷이 없습니다.", "missing_source_snapshot")
    scoped = select_words(frozen_bank, worksheet["selected_unit_ids"], allow_partial=worksheet.get("partial_selection", False))
    used_ids = {question["source_word_id"] for question in worksheet["questions"]}
    used_words = {_canonical(question["word"]) for question in worksheet["questions"]}
    eligible = [word for word in scoped if word["id"] not in used_ids and _canonical(word["word"]) not in used_words]
    if not eligible:
        raise ExamError("선택한 범위의 모든 단어가 이미 출제되어 다른 단어로 바꿀 수 없습니다.", "no_replacement_available")
    replacement = generate_writing_questions([_rng(seed).choice(eligible)], frozen_bank)[0]
    replacement["id"] = question_id
    result = deepcopy(worksheet)
    result["questions"] = [replacement if question["id"] == question_id else question for question in result["questions"]]
    result["source_entries"] = [deepcopy(entry) for question in result["questions"] for entry in question["source_snapshot"]["source_entries"]]
    result["generation_bank_snapshot"] = deepcopy(frozen_bank)
    result["pdfs_stale"] = True
    validate_writing_worksheet(result, require_reviewed=False)
    return result
