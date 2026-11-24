"""Meaning/source/answer invariants without an AI service or credentials."""
from copy import deepcopy
import json
import unittest

from feature_word_meaning.exams import (
    ExamError, add_candidate, answer_numbers, apply_semantic_review,
    build_generation_request, build_review_request, confirm_question_review,
    create_worksheet, edit_question, parse_generation_response,
    question_content_hash, reorder_choices, select_candidate, select_words,
    validate_question, validate_worksheet,
)


def bank_fixture():
    rows = [
        ("u1", "sound", "소리"), ("u1", "apple", "사과"),
        ("u2", "sound", "건전한"), ("u2", "look up", "찾아보다"),
        ("u3", "sound", "해협"), ("u2", "US", "미국"), ("u2", "us", "우리를"),
    ]
    return {
        "schema_version": 1, "source": {"filename": "원본.pdf", "sha256": "source-hash", "format": "pdf"},
        "units": [{"id": f"u{i}", "label": f"{i}강", "order": i, "pages": [i]} for i in range(1, 4)],
        "entries": [{"id": f"e{i}", "unit_id": unit, "source_number": i + 1,
                     "word": word, "meaning_raw": meaning, "meaning": meaning,
                     "status": "ready", "issues": [], "source": {"page": i + 1, "bbox": [1, 2, 3, 4]}}
                    for i, (unit, word, meaning) in enumerate(rows)], "counts": {},
    }


def generated(words):
    questions = []
    for word in words:
        groups = {}
        for source in word["source_meanings"]:
            text = source["meaning_raw"]
            groups.setdefault(text, {"source_spans": []})["source_spans"].append(
                {"entry_id": source["entry_id"], "start": 0, "end": len(text), "quote": text})
        count = 5 - len(groups)
        questions.append({"word_id": word["id"], "groups": list(groups.values()),
                          "distractors": [{"text": f"시험 검증용 틀린 뜻 {i}"} for i in range(count)]})
    return {"questions": questions}


def review_response(questions):
    reviews = []
    for question in questions:
        correct = set(question["correct_choice_ids"])
        reviews.append({
            "question_id": question["id"], "content_hash": question_content_hash(question),
            "source_coverage_complete": True, "grouping_is_accurate": True,
            "correct_choices_use_only_source": True, "reason": "별도 검수에서 원문과 보기를 대조함",
            "distractors": [{"choice_id": c["id"], "is_valid_meaning": False,
                              "overlaps_correct": False, "reason": "단어의 유효한 의미가 아님"}
                             for c in question["choices"] if c["id"] not in correct],
        })
    return {"reviews": reviews}


class SelectionTests(unittest.TestCase):
    def test_selected_scope_unions_senses_without_changing_bank(self):
        bank = bank_fixture()
        before = deepcopy(bank)
        words = select_words(bank, ["u2", "u1"])
        self.assertEqual([w["word"] for w in words], ["sound", "apple", "look up", "US", "us"])
        sound = words[0]
        self.assertEqual(sound["meaning_raw"], "소리\n건전한")
        self.assertEqual(sound["source_entry_ids"], ["e0", "e2"])
        self.assertNotIn("해협", sound["meaning_raw"])
        words[0]["source_entries"][0]["word"] = "변경"
        self.assertEqual(bank, before)

    def test_exact_case_and_phrases_are_distinct(self):
        bank = bank_fixture()
        bank["entries"].extend([
            dict(bank["entries"][0], id="e8", word="soundly"),
            dict(bank["entries"][0], id="e9", word=" sound "),
        ])
        words = select_words(bank, ["u1", "u2"])
        self.assertEqual(len(words), 6)
        self.assertEqual(words[0]["source_entry_ids"], ["e0", "e9", "e2"])

    def test_review_block_and_explicit_partial(self):
        bank = bank_fixture()
        bank["entries"][1]["status"] = "review"
        with self.assertRaises(ExamError) as caught:
            select_words(bank, ["u1"])
        self.assertEqual(caught.exception.code, "review_required")
        self.assertEqual(caught.exception.details["review_count"], 1)
        self.assertEqual([w["word"] for w in select_words(bank, ["u1"], allow_partial=True)], ["sound"])

    def test_sampling_is_deterministic_without_replacement_after_merge(self):
        bank = bank_fixture()
        a = select_words(bank, ["u1", "u2"], count=4, seed=9)
        b = select_words(bank, ["u1", "u2"], count=4, seed=9)
        self.assertEqual(a, b)
        self.assertEqual(len({w["id"] for w in a}), 4)
        with self.assertRaises(ExamError):
            select_words(bank, ["u1", "u2"], count=6)

    def test_no_twenty_question_cap(self):
        bank = bank_fixture()
        bank["entries"] = [dict(bank["entries"][0], id=f"e{i}", word=f"word {i}") for i in range(55)]
        self.assertEqual(len(select_words(bank, ["u1"], count=45, seed=1)), 45)

    def test_corrected_meaning_used_while_original_is_preserved(self):
        bank = bank_fixture()
        bank["entries"][0]["meaning_raw"] = "소리 OCR 잘못 추출"
        bank["entries"][0]["meaning"] = "소리"
        words = select_words(bank, ["u1"])
        self.assertEqual(words[0]["source_meanings"][0]["meaning_raw"], "소리")
        self.assertEqual(words[0]["source_meanings"][0]["original_meaning_raw"], "소리 OCR 잘못 추출")
        questions = parse_generation_response(words, generated(words))
        worksheet = create_worksheet(bank, ["u1"], questions)
        validate_worksheet(worksheet, require_reviewed=False)
        self.assertEqual(worksheet["source_entries"][0]["meaning_raw"], "소리 OCR 잘못 추출")

    def test_bad_inputs_are_rejected(self):
        bank = bank_fixture()
        for kwargs in [{"count": True}, {"count": 0}, {"count": 1.2}, {"shuffle": 1}, {"allow_partial": 1}, {"seed": True}]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ExamError):
                select_words(bank, ["u1"], **kwargs)
        for units in [[], ["missing"], ["u1", "u1"]]:
            with self.subTest(units=units), self.assertRaises(ExamError):
                select_words(bank, units)


class GenerationTests(unittest.TestCase):
    def setUp(self):
        self.bank = bank_fixture()
        self.words = select_words(self.bank, ["u1", "u2"])
        self.response = generated(self.words)

    def test_request_contains_only_selected_source_and_no_unverified_distractors(self):
        request = build_generation_request(self.words)
        self.assertIn("source_meanings", request["user_prompt"])
        self.assertNotIn("해협", request["user_prompt"])
        self.assertIn("원본에 없는 유효한 뜻도 오답으로 쓰지", request["system_prompt"])
        parsed = json.loads(request["user_prompt"])
        self.assertNotIn("distractors", parsed["words"][0])

    def test_multi_correct_choices_and_frozen_source(self):
        questions = parse_generation_response(self.words, self.response, seed=4)
        sound = questions[0]
        self.assertEqual(len(sound["choices"]), 5)
        self.assertEqual(len(sound["correct_choice_ids"]), 2)
        self.assertEqual(sound["review_status"], "needs_review")
        self.assertEqual(set(c["text"] for c in sound["choices"] if c["id"] in sound["correct_choice_ids"]), {"소리", "건전한"})
        self.words[0]["source_meanings"][0]["meaning_raw"] = "원본 변경"
        self.assertEqual(sound["source_meanings"][0]["meaning_raw"], "소리")

    def test_missing_duplicate_and_out_of_scope_question_rejected(self):
        bad_cases = []
        missing = deepcopy(self.response)
        missing["questions"].pop()
        bad_cases.append(missing)
        duplicate = deepcopy(self.response)
        duplicate["questions"].append(deepcopy(duplicate["questions"][0]))
        bad_cases.append(duplicate)
        alien = deepcopy(self.response)
        alien["questions"][0]["word_id"] = "out-of-scope"
        bad_cases.append(alien)
        for data in bad_cases:
            with self.assertRaises(ExamError) as caught:
                parse_generation_response(self.words, data)
            self.assertEqual(caught.exception.code, "incomplete_generation")

    def test_forged_missing_overlapping_and_empty_source_spans_rejected(self):
        for mutation in ("wrong_quote", "missing_sense", "overlap", "empty"):
            data = deepcopy(self.response)
            group = data["questions"][0]["groups"][0]
            if mutation == "wrong_quote":
                group["source_spans"][0]["quote"] = "가짜 정답"
            elif mutation == "missing_sense":
                data["questions"][0]["groups"].pop()
            elif mutation == "overlap":
                group["source_spans"].append(deepcopy(group["source_spans"][0]))
            else:
                group["source_spans"] = []
            with self.subTest(mutation=mutation), self.assertRaises(ExamError):
                parse_generation_response(self.words, data)

    def test_original_single_row_multiple_senses_can_be_grouped(self):
        bank = bank_fixture()
        bank["entries"][0]["meaning_raw"] = "n. 소리, a. 건전한"
        bank["entries"][0]["meaning"] = bank["entries"][0]["meaning_raw"]
        words = [w for w in select_words(bank, ["u1"]) if w["word"] == "sound"]
        text = words[0]["source_meanings"][0]["meaning_raw"]
        data = {"questions": [{"word_id": words[0]["id"], "groups": [
            {"source_spans": [{"entry_id": "e0", "start": 3, "end": 5, "quote": text[3:5]}]},
            {"source_spans": [{"entry_id": "e0", "start": 10, "end": 13, "quote": text[10:13]}]},
        ], "distractors": ["가짜 하나", "가짜 둘", "가짜 셋"]}]}
        question = parse_generation_response(words, data)[0]
        self.assertEqual(len(question["correct_choice_ids"]), 2)

    def test_duplicate_or_wrong_count_choices_rejected(self):
        for mode in ("same_wrong", "same_correct", "too_few", "too_many"):
            data = deepcopy(self.response)
            distractors = data["questions"][0]["distractors"]
            if mode == "same_wrong":
                distractors[1] = deepcopy(distractors[0])
            elif mode == "same_correct":
                distractors[0]["text"] = "소리"
            elif mode == "too_few":
                distractors.pop()
            else:
                distractors.append({"text": "가짜 추가"})
            with self.subTest(mode=mode), self.assertRaises(ExamError):
                parse_generation_response(self.words, data)

    def test_source_only_correct_text_ignores_model_added_prose(self):
        data = deepcopy(self.response)
        data["questions"][0]["groups"][0]["text"] = "원본에 없는 뜻"
        question = parse_generation_response(self.words, data)[0]
        self.assertNotIn("원본에 없는 뜻", [c["text"] for c in question["choices"]])

    def test_malformed_external_response_raises_domain_error(self):
        malformed = [[], {"questions": None}, {"questions": [None]}, {"questions": [{"word_id": []}]}]
        bad = deepcopy(self.response)
        bad["questions"][0]["groups"][0]["source_spans"][0]["entry_id"] = []
        malformed.append(bad)
        for response in malformed:
            with self.subTest(response=response), self.assertRaises(ExamError):
                parse_generation_response(self.words, response)


class ReviewAndSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.bank = bank_fixture()
        self.words = select_words(self.bank, ["u1", "u2"], count=3, seed=8)
        self.questions = parse_generation_response(self.words, generated(self.words), seed=3)

    def test_generation_is_draft_and_separate_review_is_required(self):
        worksheet = create_worksheet(self.bank, ["u1", "u2"], self.questions, selected_words=self.words)
        with self.assertRaises(ExamError) as caught:
            validate_worksheet(worksheet)
        self.assertEqual(caught.exception.code, "review_required")
        request = build_review_request(self.questions)
        self.assertIn("원문 밖의 유효한 뜻도 오답으로 허용하지", request["system_prompt"])
        questions = apply_semantic_review(self.questions, review_response(self.questions), "독립 검수 모델")
        worksheet = create_worksheet(self.bank, ["u1", "u2"], questions, selected_words=self.words)
        validate_worksheet(worksheet)
        self.assertEqual(worksheet["worksheet_kind"], "word_meaning_test")
        self.bank["entries"][0]["meaning_raw"] = "변경한 원문"
        validate_worksheet(worksheet)

    def test_valid_alternative_sense_is_rejected_by_independent_review(self):
        review = review_response(self.questions)
        review["reviews"][0]["distractors"][0]["is_valid_meaning"] = True
        review["reviews"][0]["distractors"][0]["reason"] = "원문 밖에 있지만 단어의 다른 유효한 뜻"
        with self.assertRaises(ExamError) as caught:
            apply_semantic_review(self.questions, review, "검수 모델")
        self.assertEqual(caught.exception.code, "review_rejected")
        self.assertEqual(len(caught.exception.details["rejected"]), 1)
        self.assertEqual(len(caught.exception.details["accepted"]), 2)

    def test_wrong_review_hash_and_missing_verdict_rejected(self):
        for mode in ("hash", "missing", "string_boolean", "overlap"):
            review = review_response(self.questions)
            first = review["reviews"][0]
            if mode == "hash":
                first["content_hash"] = "different-content"
            elif mode == "missing":
                first["distractors"].pop()
            elif mode == "string_boolean":
                first["source_coverage_complete"] = "true"
            else:
                first["distractors"][0]["overlaps_correct"] = True
            with self.subTest(mode=mode), self.assertRaises(ExamError):
                apply_semantic_review(self.questions, review, "검수 모델")

    def test_reorder_preserves_answer_ids_and_review(self):
        question = confirm_question_review(self.questions[0])
        ids = list(reversed([c["id"] for c in question["choices"]]))
        reordered = reorder_choices(question, ids)
        self.assertEqual(set(reordered["correct_choice_ids"]), set(question["correct_choice_ids"]))
        self.assertEqual(question_content_hash(reordered), question_content_hash(question))
        validate_question(reordered, require_reviewed=True)
        self.assertEqual(answer_numbers(reordered), [i for i, c in enumerate(reordered["choices"], 1) if c["id"] in question["correct_choice_ids"]])

    def test_edit_invalidates_review_and_cannot_silently_change_correct_meaning(self):
        question = confirm_question_review(self.questions[0])
        choices = deepcopy(question["choices"])
        wrong = next(c for c in choices if c["id"] not in question["correct_choice_ids"])
        wrong["text"] = "새로 바꾼 오답"
        edited = edit_question(question, choices, question["correct_choice_ids"])
        self.assertEqual(edited["review_status"], "needs_review")
        self.assertIsNone(edited["review_evidence"])
        with self.assertRaises(ExamError):
            validate_question(edited, require_reviewed=True)
        choices = deepcopy(question["choices"])
        next(c for c in choices if c["id"] in question["correct_choice_ids"])["text"] = "원문에 없는 정답"
        with self.assertRaises(ExamError):
            edit_question(question, choices, question["correct_choice_ids"])

    def test_review_cannot_be_reused_after_raw_mutation(self):
        question = confirm_question_review(self.questions[0])
        wrong = next(c for c in question["choices"] if c["id"] not in question["correct_choice_ids"])
        wrong["text"] = "내용 변조"
        with self.assertRaises(ExamError) as caught:
            validate_question(question, require_reviewed=True)
        self.assertEqual(caught.exception.code, "review_required")

    def test_wrong_only_edit_keeps_existing_pos_distinctions_and_source_groups(self):
        bank = bank_fixture()
        bank['entries'][2]['meaning'] = bank['entries'][2]['meaning_raw'] = 'n. 소리'
        words = [w for w in select_words(bank, ['u1', 'u2']) if w['word'] == 'sound']
        question = parse_generation_response(words, generated(words))[0]
        choices = deepcopy(question['choices'])
        next(c for c in choices if c['id'] not in question['correct_choice_ids'])['text'] = '수정한 오답'
        edited = edit_question(question, choices, question['correct_choice_ids'])
        self.assertEqual(edited['semantic_groups'], question['semantic_groups'])
        validate_question(edited)

    def test_displayed_word_edit_keeps_source_and_requires_fresh_review(self):
        question = confirm_question_review(self.questions[0])
        edited = edit_question(question, question["choices"], question["correct_choice_ids"], word="correctedword")
        self.assertEqual(edited["source_word"], question["word"])
        self.assertEqual(edited["original_word"], question["word"])
        self.assertEqual(edited["word"], "correctedword")
        self.assertTrue(edited["word_edited"])
        worksheet = create_worksheet(self.bank, ["u1", "u2"], self.questions, selected_words=self.words)
        worksheet["questions"][0] = edited
        validate_worksheet(worksheet, require_reviewed=False)
        with self.assertRaises(ExamError):
            validate_worksheet(worksheet)
        original = self.words[0]
        regenerated_word = dict(original, word=edited["word"], source_word=edited["source_word"])
        candidate = parse_generation_response([regenerated_word], generated([regenerated_word]))[0]
        self.assertEqual(candidate["source_word"], question["word"])
        self.assertEqual(len(add_candidate(edited, candidate)["candidates"]), 2)

    def test_json_roundtrip_keeps_question_order_and_answer_numbers(self):
        questions = [confirm_question_review(q) for q in self.questions]
        worksheet = create_worksheet(self.bank, ["u1", "u2"], questions, selected_words=self.words)
        reopened = json.loads(json.dumps(worksheet, ensure_ascii=False))
        validate_worksheet(reopened)
        self.assertEqual([q["id"] for q in reopened["questions"]], [q["id"] for q in worksheet["questions"]])
        self.assertEqual([answer_numbers(q) for q in reopened["questions"]], [answer_numbers(q) for q in questions])

    def test_wrong_scope_or_missing_frozen_source_rejected(self):
        questions = [confirm_question_review(q) for q in self.questions]
        worksheet = create_worksheet(self.bank, ["u1", "u2"], questions, selected_words=self.words)
        worksheet["source_entries"].pop()
        with self.assertRaises(ExamError):
            validate_worksheet(worksheet)

    def test_candidate_limit_scope_and_edits_preserved(self):
        question = confirm_question_review(self.questions[0])
        for _ in range(9):
            question = add_candidate(question, self.questions[0])
        self.assertEqual(len(question["candidates"]), 10)
        with self.assertRaises(ExamError) as caught:
            add_candidate(question, self.questions[0])
        self.assertEqual(caught.exception.code, "candidate_limit")
        current = select_candidate(question, "candidate_2")
        choices = deepcopy(current["choices"])
        next(c for c in choices if c["id"] not in current["correct_choice_ids"])["text"] = "수정한 후보"
        current = edit_question(current, choices, current["correct_choice_ids"])
        current = select_candidate(current, "candidate_1")
        current = select_candidate(current, "candidate_2")
        self.assertIn("수정한 후보", [c["text"] for c in current["choices"]])
        alien = deepcopy(self.questions[0])
        alien["source_word_id"] = "out-of-scope"
        with self.assertRaises(ExamError):
            add_candidate(question, alien)

    def test_regenerated_candidate_keeps_exact_source_after_display_edit(self):
        original = confirm_question_review(self.questions[0])
        original["source_snapshot"] = deepcopy(self.words[0])
        frozen = deepcopy(original["source_snapshot"])
        edited = edit_question(original, original["choices"], original["correct_choice_ids"], word="correctedword")
        working = deepcopy(frozen)
        working.update(word=edited["word"], source_word=edited["source_word"])
        candidate = confirm_question_review(parse_generation_response([working], generated([working]))[0])
        candidate["source_snapshot"] = deepcopy(working)
        candidate_before = deepcopy(candidate)
        result = add_candidate(edited, candidate)
        selected = select_candidate(result, "candidate_2")
        self.assertEqual(selected["word"], "correctedword")
        self.assertEqual(selected["source_snapshot"], frozen)
        self.assertEqual(selected["source_meanings"], original["source_meanings"])
        self.assertEqual(selected["correct_choice_ids"], candidate["correct_choice_ids"])
        self.assertEqual(selected["review_evidence"], candidate["review_evidence"])
        self.assertTrue(all(c["question"]["source_snapshot"] == frozen for c in selected["candidates"]))
        self.assertEqual(candidate, candidate_before)
        validate_question(selected, require_reviewed=True)

    def test_existing_augmented_candidate_snapshot_repairs_without_losing_history(self):
        original = confirm_question_review(self.questions[0])
        original["source_snapshot"] = deepcopy(self.words[0])
        frozen = deepcopy(original["source_snapshot"])
        other = reorder_choices(original, list(reversed([c["id"] for c in original["choices"]])))
        current = select_candidate(add_candidate(original, other), "candidate_2")
        current["source_snapshot"]["source_word"] = original["source_word"]
        current["candidates"][1]["question"]["source_snapshot"]["source_word"] = original["source_word"]
        before = deepcopy(current)
        restored = select_candidate(select_candidate(current, "candidate_1"), "candidate_2")
        self.assertEqual(restored["source_snapshot"], frozen)
        self.assertEqual(restored["choices"], before["choices"])
        self.assertEqual(restored["correct_choice_ids"], before["correct_choice_ids"])
        self.assertEqual(restored["review_evidence"], before["review_evidence"])
        self.assertEqual([c["id"] for c in restored["candidates"]], ["candidate_1", "candidate_2"])
        self.assertTrue(all(c["question"]["source_snapshot"] == frozen for c in restored["candidates"]))
        self.assertEqual(current, before)
        validate_question(restored, require_reviewed=True)

    def test_candidate_snapshot_repair_rejects_changes_to_frozen_source_data(self):
        original = confirm_question_review(self.questions[0])
        original["source_snapshot"] = deepcopy(self.words[0])
        result = add_candidate(original, original)
        for field in ["source_entries", "source_entry_ids", "meaning_raw", "original_meaning_raw", "unit_ids"]:
            with self.subTest(field=field):
                corrupted = deepcopy(result)
                corrupted["candidates"][1]["question"]["source_snapshot"][field] = "changed"
                with self.assertRaises(ExamError) as caught:
                    select_candidate(corrupted, "candidate_2")
                self.assertEqual(caught.exception.code, "invalid_candidate")


if __name__ == "__main__":
    unittest.main()
