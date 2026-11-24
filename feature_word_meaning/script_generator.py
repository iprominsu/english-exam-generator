"""Generate source-backed word/meaning questions without any model calls.

Explicit top-level separators, including commas, separate source meanings.
At most five choices retain every meaning, including five correct choices.
Surface-text
filters reduce obvious distractor collisions; they do not establish that an
unseen dictionary sense or a differently written synonym is invalid. Therefore
every generated question remains ``needs_review`` until a person confirms it.
"""

from __future__ import annotations

from copy import deepcopy
from difflib import SequenceMatcher
from functools import lru_cache
import re
from types import MappingProxyType
import unicodedata

from .exams import (
    ExamError, _POS, _canonical, _group_text, _required_offsets, _rng,
    _validate_words, parse_generation_response, question_content_hash,
    GENERATED_CHOICE_COUNT, MAX_GENERATED_CORRECT,
    choice_display_width,
)

GENERATOR_VERSION = 5
COMPACT_WIDTH_TIERS = (16, 20, 28, 40)
_OPEN_BRACKETS = {"(": ")", "[": "]", "{": "}", "（": "）", "【": "】"}
_COMPONENT_SEPARATOR = re.compile(r"[,，;/／；\n·ㆍ]|[()\[\]{}（）【】]")
_KOREAN_POS_WORD = r"(?:명사|동사|형용사|부사|숙어|대명사|전치사|접속사|감탄사|조동사|관형사|수사|자동사|타동사|명|동|형|부|숙|대|전|접|감|조|관|수|자|타)"
_KOREAN_POS = re.compile(r"[\[【]\s*" + _KOREAN_POS_WORD + r"(?:\s*[/,·ㆍ]\s*" + _KOREAN_POS_WORD + r")*\s*[\]】]")


def _source_segments(text: str) -> list[tuple[int, int]]:
    """Split only explicit top-level meaning boundaries; keep exact offsets.

    Parentheses and brackets can contain slashes (e.g. ``(사람/사물)``).
    Commas split listed meanings; parentheses retain their complete expression.
    Fragments containing only punctuation or a POS label are not answer choices.
    """
    stack: list[str] = []
    boundaries = [0]
    for index, char in enumerate(text):
        if char in _OPEN_BRACKETS:
            stack.append(_OPEN_BRACKETS[char])
        elif stack and char == stack[-1]:
            stack.pop()
        elif not stack and char in ",，/／;；\n·ㆍ":
            boundaries.extend((index, index + 1))
    boundaries.append(len(text))
    segments = []
    for start, end in zip(boundaries[::2], boundaries[1::2]):
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end and _required_offsets(text[start:end]):
            segments.append((start, end))
    # A row consisting solely of separators/POS labels cannot be a valid answer.
    if not segments:
        raise ExamError("등록된 뜻에 출제할 내용이 없습니다.", "invalid_selection")
    return segments


def _groups(source_meanings: list[dict]) -> list[dict]:
    grouped: dict[str, dict] = {}
    for source in source_meanings:
        raw = source["meaning_raw"]
        for start, end in _source_segments(raw):
            span = {"entry_id": source["entry_id"], "start": start, "end": end, "quote": raw[start:end]}
            # Equality is textual, not a claim that differently written phrases
            # are synonymous. Keep each occurrence's provenance for coverage.
            key = _canonical(span["quote"]).strip(" ,;/·ㆍ").casefold()
            grouped.setdefault(key, {"source_spans": []})["source_spans"].append(span)
    return list(grouped.values())


@lru_cache(maxsize=16384)
def _comparison_text(text: str) -> str:
    text = _KOREAN_POS.sub("", _POS.sub("", unicodedata.normalize("NFKC", text))).casefold()
    return "".join(char for char in text if char.isalnum())


@lru_cache(maxsize=16384)
def _comparison_parts(text: str) -> tuple[str, ...]:
    # Check both the complete expression and comma-separated surface phrases.
    # POS labels are metadata. Remove complete labels before splitting so that
    # a bare fragment such as '명' cannot link every noun's meaning together.
    comparison = _KOREAN_POS.sub("", _POS.sub("", unicodedata.normalize("NFKC", text)))
    return tuple(dict.fromkeys(part for part in (
        _comparison_text(comparison), *(_comparison_text(t) for t in _COMPONENT_SEPARATOR.split(comparison))
    ) if part))


@lru_cache(maxsize=16384)
def _character_counts(text: str):
    counts: dict[str, int] = {}
    for char in text:
        counts[char] = counts.get(char, 0) + 1
    return MappingProxyType(counts)


@lru_cache(maxsize=32768)
def _similarity_score(a: str, b: str, minimum: float) -> float:
    """Keep exact qualifying ratios; reject only impossible upper bounds.

    A matching subsequence cannot exceed either string's length or the shared
    character multiset. These are the same bounds as SequenceMatcher's quick
    ratios, with character counts reused across comparisons. Ordered keys are
    intentional: SequenceMatcher's final ratio can depend on argument order.
    All caches contain only immutable values derived from their full inputs;
    no bank, entry, question or mutable source snapshot is retained.
    """
    total = len(a) + len(b)
    if not total:
        return 1.0
    if 2.0 * min(len(a), len(b)) / total < minimum:
        return 0.0
    left, right = _character_counts(a), _character_counts(b)
    common = sum(min(count, right.get(char, 0)) for char, count in left.items())
    if 2.0 * common / total < minimum:
        return 0.0
    score = SequenceMatcher(None, a, b, autojunk=False).ratio()
    return score if score >= minimum else 0.0


@lru_cache(maxsize=32768)
def _conflicts(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    for a in left:
        for b in right:
            if a == b or (min(len(a), len(b)) >= 2 and (a in b or b in a)):
                return True
            # This catches spelling/spacing variants, not semantic equivalence.
            if min(len(a), len(b)) >= 3 and _similarity_score(a, b, 0.8) >= 0.8:
                return True
    return False


def _answer_groups(source_meanings: list[dict]) -> list[dict]:
    groups = _groups(source_meanings)
    # Distinct listed meanings each get a choice. Only the five-choice limit
    # permits packing; preserve all occurrences and exact source offsets.
    if len(groups) > MAX_GENERATED_CORRECT:
        total = len(groups)
        groups = [{"source_spans": [span for group in groups[i * total // MAX_GENERATED_CORRECT:(i + 1) * total // MAX_GENERATED_CORRECT]
                                    for span in group["source_spans"]]}
                  for i in range(MAX_GENERATED_CORRECT)]
    return groups


@lru_cache(maxsize=16384)
def _display_width(text: str) -> int:
    """Approximate print width in half-width units without PDF dependencies."""
    return choice_display_width(text)


def _bank_pool(bank: dict) -> tuple[list[dict], list[dict]]:
    if not isinstance(bank, dict) or not isinstance(bank.get("entries"), list):
        raise ExamError("단어은행 형식이 올바르지 않습니다.", "invalid_selection")
    entries = bank["entries"]
    pool: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ExamError("단어은행 항목 형식이 올바르지 않습니다.", "invalid_selection")
        if entry.get("status") != "ready":
            continue
        entry_id, raw, word = entry.get("id"), entry.get("meaning", entry.get("meaning_raw")), entry.get("word")
        if not all(isinstance(value, str) and value.strip() for value in (entry_id, raw, word)):
            # Optional distractor material needs a valid source ID/word/text.
            # Skip unsuitable rows without editing them; selected target rows
            # still pass select_words/_validate_words and _groups separately.
            continue
        try:
            groups = _groups([{"entry_id": entry_id, "meaning_raw": raw}])
        except ExamError as exc:
            if exc.code != "invalid_selection":
                raise
            # A saved nonempty value such as ``n.`` or ``/`` contains no
            # usable meaning. It cannot block unrelated targets as a pool row.
            continue
        for group in groups:
            text = _group_text(group["source_spans"])
            key = _canonical(text).casefold()
            item = pool.setdefault(key, {"text": text, "source_spans": [], "source_entry_ids": [], "source_words": [],
                                         "parts": _comparison_parts(text), "context_parts": ()})
            # A split phrase may be a synonym of another phrase in its source
            # row (e.g. 떠받치다, 지탱하다). Retain that context as a conservative
            # exclusion signal, even though choices display separate meanings.
            item["context_parts"] = tuple(dict.fromkeys((*item["context_parts"], *_comparison_parts(raw))))
            item["source_spans"].extend(group["source_spans"])
            if entry_id not in item["source_entry_ids"]:
                item["source_entry_ids"].append(entry_id)
            if word not in item["source_words"]:
                item["source_words"].append(word)
    return list(pool.values()), entries


def _known_meanings(word: dict, entries: list[dict]) -> tuple[tuple[str, ...], list[str]]:
    target_words = {_canonical(word["word"]).casefold(), _canonical(word.get("source_word", word["word"])).casefold()}
    texts = [source["meaning_raw"] for source in word["source_meanings"]]
    entry_ids = []
    # A known same-word sense is excluded even when it is outside the selected
    # units or is an unconfirmed/excluded row. It is never added as an answer.
    for entry in entries:
        entry_word = entry.get("word")
        if not isinstance(entry_word, str) or _canonical(entry_word).casefold() not in target_words:
            continue
        if isinstance(entry.get("id"), str):
            entry_ids.append(entry["id"])
        texts.extend(entry[key] for key in ("meaning", "meaning_raw") if isinstance(entry.get(key), str))
    parts = tuple(dict.fromkeys(part for text in texts for part in _comparison_parts(text)))
    return parts, list(dict.fromkeys(entry_ids))


def _confusion_score(word: str, candidate: dict) -> float:
    """Rank meanings of similarly spelled bank words, without semantic claims."""
    target = _comparison_text(word)
    best = 0.0
    for source_word in candidate["source_words"]:
        other = _comparison_text(source_word)
        if min(len(target), len(other)) < 3 or target == other:
            continue
        score = _similarity_score(target, other, 0.6)
        if score >= 0.6:
            best = max(best, score)
    return best


def _choose_distractors(pool: list[dict], known: tuple[str, ...], required: int, rng, word: str) -> list[dict]:
    if required == 0:
        return []
    # One bounded expansion also excludes linked alternatives: if the bank
    # lists 떠받치다/지탱하다 and 유지하다/지탱하다, neither 지탱하다 nor 유지하다
    # is a safe distractor for a question whose answer includes 떠받치다.
    # Do not recursively turn polysemous rows into a semantic equivalence graph.
    known_set = set(known)
    related = tuple(dict.fromkeys((*known, *(part for candidate in pool
                                             if known_set.intersection(candidate["context_parts"])
                                             for part in candidate["context_parts"]))))
    eligible = [candidate for candidate in pool if not _conflicts(candidate["context_parts"], related)]
    rng.shuffle(eligible)
    widths = {id(candidate): _display_width(candidate["text"]) for candidate in eligible}
    scores = {id(candidate): _confusion_score(word, candidate) for candidate in eligible}
    # Compact options usually fit the unchanged PDF's two-column choice grid.
    # Randomize within each width tier; only widen the pool when the shorter
    # candidates cannot provide enough mutually nonconflicting distractors.
    def width_tier(candidate):
        width = widths[id(candidate)]
        return next((index for index, limit in enumerate(COMPACT_WIDTH_TIERS) if width <= limit), len(COMPACT_WIDTH_TIERS))

    eligible.sort(key=lambda candidate: (-scores[id(candidate)], width_tier(candidate), widths[id(candidate)] if widths[id(candidate)] > COMPACT_WIDTH_TIERS[-1] else 0))
    best: list[dict] = []

    def conflicts(left, right):
        return (_conflicts(left["parts"], right["context_parts"])
                or _conflicts(left["context_parts"], right["parts"]))

    def choose(candidates: list[dict], start: int, selected: list[dict]) -> list[dict] | None:
        nonlocal best
        if len(selected) > len(best):
            best = selected[:]
        if len(selected) == required:
            return selected
        if len(selected) + len(candidates) - start <= len(best):
            return None
        for index in range(start, len(candidates)):
            candidate = candidates[index]
            if any(conflicts(candidate, other) for other in selected):
                continue
            result = choose(candidates, index + 1, [*selected, candidate])
            if result is not None:
                return result
        return None

    def fill(selected):
        previous_count = -1
        for limit in (*COMPACT_WIDTH_TIERS, None):
            candidates = [candidate for candidate in eligible
                          if (limit is None or widths[id(candidate)] <= limit)
                          and all(not conflicts(candidate, other) for other in selected)]
            if len(candidates) == previous_count:
                continue
            previous_count = len(candidates)
            result = choose(candidates, 0, selected)
            if result is not None:
                return result
        return None

    # Include a spelling-confusable meaning whenever a complete safe set can
    # contain it, even when that meaning is longer than the compact fallback.
    result = None
    for candidate in eligible:
        if scores[id(candidate)] <= 0:
            break
        result = fill([candidate])
        if result is not None:
            break
    if result is None:
        result = fill([])
    if result is None:
        raise ExamError(
            f"'{word}'에 사용할 서로 다른 오답이 부족합니다. 단어은행에 뜻을 더 등록한 후 다시 생성하세요.",
            "insufficient_distractors", word=word, required=required, available=len(best), candidate_count=len(eligible),
        )
    return result


def generate_script_questions(words: list[dict], bank: dict, seed: int | str | None = None) -> list[dict]:
    """Build questions with source-only answers and bank-sourced distractors.

    The caller selects/snapshots the target words. The entire bank is used only
    for distractor material and to exclude known out-of-scope senses; it never
    expands the question's selected answer scope. No model/provider is used.
    """
    _validate_words(words)
    rng = _rng(seed)
    pool, entries = _bank_pool(bank)
    generated, evidence = [], []
    for word in words:
        groups = _answer_groups(word["source_meanings"])
        known, excluded_ids = _known_meanings(word, entries)
        distractors = _choose_distractors(pool, known, GENERATED_CHOICE_COUNT - len(groups), rng, word["word"])
        generated.append({"word_id": word["id"], "groups": groups, "distractors": [{"text": item["text"]} for item in distractors]})
        evidence.append((distractors, excluded_ids))
    # Reuse all existing exact-source, completeness, choice-count and ID checks.
    questions = parse_generation_response(words, {"questions": generated}, seed=seed)
    for question, (distractors, excluded_ids) in zip(questions, evidence):
        by_text = {choice["text"]: choice["id"] for choice in question["choices"]}
        question["generation_method"] = "script"
        question["generation_evidence"] = {
            "method": "bank_script", "version": GENERATOR_VERSION, "seed": seed,
            "grouping": "each_listed_meaning_up_to_five_all_correct_allowed",
            "choice_order": "ascending_display_width_randomized_ties",
            "distractor_selection": "confusable_bank_word_first_then_compact_width_tiers",
            "text_conflict_filter": "known_senses_and_bank_row_context_equality_containment_similarity",
            "semantic_review_performed": False,
            "excluded_same_word_entry_ids": excluded_ids,
            "distractors": [{"choice_id": by_text[item["text"]], "text": item["text"],
                             "source_entry_ids": list(item["source_entry_ids"]),
                             "source_words": list(item["source_words"]),
                             "confusion_score": _confusion_score(question["word"], item),
                             "source_spans": deepcopy(item["source_spans"])} for item in distractors],
            "content_hash": question_content_hash(question),
        }
    return questions
