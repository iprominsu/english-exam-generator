"""Source coverage and distractor safeguards for model-free generation."""

from copy import deepcopy
import unittest
from unittest.mock import patch

from feature_word_meaning.exams import (
    ExamError, add_candidate, create_worksheet, select_candidate, select_words,
    validate_question, validate_worksheet,
)
from feature_word_meaning.script_generator import _display_width, generate_script_questions


def bank_fixture(rows=None):
    rows = rows or [
        ("u1", "sound", "소리, 음향 / 건전한", "ready"),
        ("u1", "apple", "사과", "ready"),
        ("u1", "river", "강", "ready"),
        ("u1", "tree", "나무", "ready"),
        ("u2", "sound", "해협", "ready"),
        ("u2", "noisy", "소리 지르는", "ready"),
        ("u2", "noise", "음향", "ready"),
        ("u2", "healthy", "건전한", "ready"),
        ("u2", "SOUND", "튼튼한", "review"),
        ("u2", "strong", "튼튼한", "ready"),
        ("u2", "wrong review", "확인 전 뜻", "review"),
        ("u2", "excluded", "제외한 뜻", "excluded"),
    ]
    return {
        "schema_version": 1, "source": {"filename": "등록단어.pdf"},
        "units": [{"id": "u1", "label": "1강", "order": 1}, {"id": "u2", "label": "2강", "order": 2}],
        "entries": [{"id": f"e{i}", "unit_id": unit, "word": word,
                     "meaning": meaning, "meaning_raw": meaning, "status": status}
                    for i, (unit, word, meaning, status) in enumerate(rows)],
    }


def target(bank, word="sound", units=None):
    return next(w for w in select_words(bank, units or ["u1"], allow_partial=True) if w["word"] == word)


def correct_texts(question):
    return {c["text"] for c in question["choices"] if c["id"] in question["correct_choice_ids"]}


def wrong_texts(question):
    return {c["text"] for c in question["choices"] if c["id"] not in question["correct_choice_ids"]}


class ScriptGeneratorTests(unittest.TestCase):
    def test_new_choices_are_sorted_short_to_long_without_losing_answer_ids(self):
        from feature_word_meaning.exams import choice_display_width
        bank = bank_fixture()
        for seed in range(6):
            q = generate_script_questions([target(bank)], bank, seed=seed)[0]
            lengths = [choice_display_width(c['text']) for c in q['choices']]
            self.assertEqual(lengths, sorted(lengths))
            self.assertEqual(len(q['choices']), 5)
            self.assertEqual(correct_texts(q), {'소리', '음향', '건전한'})
            self.assertEqual({g['choice_id'] for g in q['semantic_groups']}, set(q['correct_choice_ids']))
            self.assertEqual(q['generation_evidence']['choice_order'], 'ascending_display_width_randomized_ties')
            validate_question(q)

    def test_five_choices_separate_every_sense_and_pack_only_over_five(self):
        from feature_word_meaning.exams import _required_offsets
        for count in range(1, 9):
            bank = bank_fixture()
            raw = " / ".join(["소리", "건전한", "해협", "튼튼한", "조사하다", "울리다", "수심", "건강"][0:count])
            bank['entries'][0]['meaning'] = bank['entries'][0]['meaning_raw'] = raw
            q = generate_script_questions([target(bank)], bank, seed=4)[0]
            self.assertEqual(len(q['choices']), 5)
            self.assertEqual(len(q['correct_choice_ids']), min(count, 5))
            self.assertEqual(len(wrong_texts(q)), max(0, 5 - count))
            covered = set()
            for group in q['semantic_groups']:
                for span in group['source_spans']:
                    covered.update(range(span['start'], span['end']))
            self.assertTrue(_required_offsets(raw) <= covered)
            validate_question(q)

    def test_similar_adjectival_and_verbal_forms_remain_separate_listed_meanings(self):
        bank = bank_fixture()
        bank['entries'][0]['meaning'] = bank['entries'][0]['meaning_raw'] = '흐르는,유동적인 / 흐르다,유동적이다'
        q = generate_script_questions([target(bank)], bank, seed=1)[0]
        self.assertEqual(len(q['choices']), 5)
        self.assertEqual(len(q['correct_choice_ids']), 4)
        self.assertEqual(correct_texts(q), {'흐르는', '유동적인', '흐르다', '유동적이다'})
        self.assertTrue(all(len(group['source_spans']) == 1 for group in q['semantic_groups']))

    def test_no_model_request_is_built_and_source_scope_stays_frozen(self):
        bank = bank_fixture()
        before = deepcopy(bank)
        words = [target(bank)]
        with patch("feature_word_meaning.exams.build_generation_request", side_effect=AssertionError("model call")), \
             patch("feature_word_meaning.exams.build_review_request", side_effect=AssertionError("model review")):
            question = generate_script_questions(words, bank, seed=9)[0]
        self.assertEqual(correct_texts(question), {"소리", "음향", "건전한"})
        self.assertEqual(question["unit_ids"], ["u1"])
        self.assertNotIn("해협", correct_texts(question))
        self.assertEqual(question["source_meanings"], words[0]["source_meanings"])
        self.assertEqual(bank, before)
        validate_question(question)

    def test_known_out_of_scope_and_unconfirmed_same_word_meanings_are_excluded(self):
        bank = bank_fixture()
        question = generate_script_questions([target(bank)], bank, seed=1)[0]
        self.assertEqual(len(wrong_texts(question)), 2)
        self.assertTrue(wrong_texts(question) <= {"사과", "강", "나무"})
        self.assertEqual(question["generation_evidence"]["excluded_same_word_entry_ids"], ["e0", "e4", "e8"])
        self.assertFalse(wrong_texts(question) & {"해협", "소리 지르는", "음향", "건전한", "튼튼한", "확인 전 뜻", "제외한 뜻"})

    def test_ready_distractors_only_and_every_distractor_has_exact_provenance(self):
        bank = bank_fixture()
        question = generate_script_questions([target(bank, "apple")], bank, seed=3)[0]
        entries = {entry["id"]: entry for entry in bank["entries"]}
        evidence = question["generation_evidence"]
        self.assertEqual(question["generation_method"], "script")
        self.assertEqual(evidence["method"], "bank_script")
        for item in evidence["distractors"]:
            self.assertIn(item["text"], wrong_texts(question))
            self.assertTrue(item["source_spans"])
            for span in item["source_spans"]:
                entry = entries[span["entry_id"]]
                self.assertEqual(entry["status"], "ready")
                self.assertEqual(entry["meaning"][span["start"]:span["end"]], span["quote"])

    def test_script_filter_never_claims_semantic_review(self):
        bank = bank_fixture()
        question = generate_script_questions([target(bank)], bank, seed=2)[0]
        self.assertEqual(question["review_status"], "needs_review")
        self.assertIsNone(question["review_evidence"])
        self.assertIs(question["generation_evidence"]["semantic_review_performed"], False)
        with self.assertRaises(ExamError) as caught:
            validate_question(question, require_reviewed=True)
        self.assertEqual(caught.exception.code, "review_required")

    def test_delimiters_split_commas_but_keep_parenthetical_slashes_in_exact_spans(self):
        bank = bank_fixture()
        bank["entries"][0]["meaning"] = bank["entries"][0]["meaning_raw"] = "n. 소리, 음향 / (사람/사물의) 건전함; 튼튼한\n해협"
        question = generate_script_questions([target(bank)], bank, seed=4)[0]
        self.assertEqual(correct_texts(question), {"n. 소리", "음향", "(사람/사물의) 건전함", "튼튼한", "해협"})
        self.assertEqual(len(question["choices"]), 5)
        raw = bank["entries"][0]["meaning"]
        spans = [span for group in question["semantic_groups"] for span in group["source_spans"]]
        self.assertTrue(all(span["quote"] == raw[span["start"]:span["end"]] for span in spans))
        validate_question(question)

    def test_duplicate_meanings_are_merged_with_all_source_occurrences(self):
        bank = bank_fixture()
        bank["entries"][0]["meaning"] = bank["entries"][0]["meaning_raw"] = "소리 / 소리"
        bank["entries"].append(dict(bank["entries"][0], id="e_duplicate", meaning=" 소리 ", meaning_raw=" 소리 "))
        question = generate_script_questions([target(bank)], bank, seed=2)[0]
        self.assertEqual(correct_texts(question), {"소리"})
        self.assertEqual(len(question["semantic_groups"][0]["source_spans"]), 3)
        self.assertEqual(len(question["choices"]), 5)
        validate_question(question)

    def test_corrected_meaning_is_used_but_original_is_retained(self):
        bank = bank_fixture()
        bank["entries"][0]["meaning_raw"] = "소리 OCR 오류"
        bank["entries"][0]["meaning"] = "소리"
        question = generate_script_questions([target(bank)], bank, seed=2)[0]
        self.assertEqual(correct_texts(question), {"소리"})
        self.assertEqual(question["original_meaning_raw"], "소리 OCR 오류")
        self.assertTrue(all(span["quote"] == "소리" for group in question["semantic_groups"] for span in group["source_spans"]))

    def test_seed_is_reproducible_and_different_seeds_can_change_choices(self):
        bank = bank_fixture()
        words = [target(bank, "apple")]
        self.assertEqual(generate_script_questions(words, bank, seed="seed"), generate_script_questions(words, bank, seed="seed"))
        outputs = {tuple(c["text"] for c in generate_script_questions(words, bank, seed=seed)[0]["choices"]) for seed in range(8)}
        self.assertGreater(len(outputs), 1)

    def test_too_few_bank_distractors_fails_without_inventing_text(self):
        bank = bank_fixture([("u1", "sound", "소리", "ready"), ("u1", "apple", "사과", "ready")])
        with self.assertRaises(ExamError) as caught:
            generate_script_questions([target(bank)], bank, seed=1)
        self.assertEqual(caught.exception.code, "insufficient_distractors")
        self.assertEqual(caught.exception.details, {"word": "sound", "required": 4, "available": 1, "candidate_count": 1})

    def test_conflicting_distractors_do_not_fill_missing_slots(self):
        bank = bank_fixture([("u1", "sound", "소리", "ready"), ("u1", "apple", "사과", "ready"),
                             ("u1", "apples", "큰 사과", "ready"), ("u1", "pie", "아주 큰 사과", "ready")])
        with self.assertRaises(ExamError) as caught:
            generate_script_questions([target(bank)], bank, seed=1)
        self.assertEqual(caught.exception.code, "insufficient_distractors")
        self.assertEqual(caught.exception.details["available"], 1)

    def test_near_identical_target_text_is_filtered(self):
        bank = bank_fixture()
        bank["entries"][0]["meaning"] = bank["entries"][0]["meaning_raw"] = "사과를 주워 담다"
        bank["entries"].append(dict(bank["entries"][1], id="similar", word="look alike", meaning="사과를 주어 담다"))
        question = generate_script_questions([target(bank)], bank, seed=1)[0]
        self.assertNotIn("사과를 주어 담다", wrong_texts(question))

    def test_multiple_words_remain_ordered_without_modifying_source_or_inputs(self):
        bank = bank_fixture()
        words = select_words(bank, ["u1"], count=4, seed=7)
        before = deepcopy(words)
        questions = generate_script_questions(words, bank, seed=5)
        self.assertEqual([q["word"] for q in questions], [w["word"] for w in words])
        self.assertEqual(words, before)
        worksheet = create_worksheet(bank, ["u1"], questions, selected_words=words)
        self.assertEqual(worksheet["generation_method"], "script")
        validate_worksheet(worksheet, require_reviewed=False)

    def test_regenerated_candidates_preserve_question_identity_and_scope(self):
        bank = bank_fixture()
        words = [target(bank)]
        first = generate_script_questions(words, bank, seed=1)[0]
        second = generate_script_questions(words, bank, seed=9)[0]
        combined = add_candidate(first, second)
        chosen = select_candidate(combined, "candidate_2")
        self.assertEqual(chosen["id"], first["id"])
        self.assertEqual(chosen["source_meanings"], first["source_meanings"])
        self.assertEqual(chosen["generation_method"], "script")
        self.assertEqual(chosen["review_status"], "needs_review")

    def test_invalid_seed_and_empty_selection_fail(self):
        bank = bank_fixture()
        with self.assertRaises(ExamError):
            generate_script_questions([target(bank)], bank, seed=True)
        with self.assertRaises(ExamError):
            generate_script_questions([], bank, seed=1)

    def test_unusable_out_of_scope_pool_rows_are_skipped_without_changes(self):
        bank = bank_fixture()
        template = bank["entries"][1]
        for index, raw in enumerate(("n.", "/", ";\n", "adv. / n.")):
            bank["entries"].append(dict(template, id=f"unused_{index}", unit_id="u2", word=f"unused {index}", meaning=raw, meaning_raw=raw))
        bank["entries"].extend([
            dict(template, id="", unit_id="u2", word="missing source id"),
            dict(template, id="missing_word", unit_id="u2", word=""),
            dict(template, id="missing_meaning", unit_id="u2", meaning=None),
        ])
        before = deepcopy(bank)
        question = generate_script_questions([target(bank, "apple")], bank, seed=1)[0]
        self.assertEqual(correct_texts(question), {"사과"})
        for item in question["generation_evidence"]["distractors"]:
            self.assertFalse(any(entry_id.startswith("unused_") or entry_id in {"", "missing_word", "missing_meaning"} for entry_id in item["source_entry_ids"]))
        self.assertEqual(bank, before)

    def test_unusable_selected_target_meaning_still_fails(self):
        for raw in ("n.", "/", "adv. / n."):
            with self.subTest(meaning=raw):
                bank = bank_fixture()
                bank["entries"][0]["meaning"] = bank["entries"][0]["meaning_raw"] = raw
                with self.assertRaises(ExamError) as caught:
                    generate_script_questions([target(bank)], bank, seed=1)
                self.assertEqual(caught.exception.code, "invalid_selection")

    def test_skipping_unusable_rows_still_reports_insufficient_distractors(self):
        bank = bank_fixture([("u1", "sound", "소리", "ready"), ("u2", "unused", "n.", "ready")])
        with self.assertRaises(ExamError) as caught:
            generate_script_questions([target(bank)], bank, seed=1)
        self.assertEqual(caught.exception.code, "insufficient_distractors")
        self.assertEqual(caught.exception.details["available"], 0)

    def test_compact_distractors_are_preferred_without_shortening_source_text(self):
        bank = bank_fixture([
            ("u1", "sound", "소리", "ready"), ("u2", "apple", "사과", "ready"),
            ("u2", "tree", "나무", "ready"), ("u2", "sea", "바다", "ready"),
            ("u2", "star", "별빛", "ready"),
            ("u2", "long", "외부 은하의 매우 길고 상세한 설명으로 이루어진 후보 문구", "ready"),
        ])
        for seed in range(8):
            question = generate_script_questions([target(bank)], bank, seed=seed)[0]
            self.assertTrue(wrong_texts(question) <= {"사과", "나무", "바다", "별빛"})
            self.assertEqual(len(wrong_texts(question)), 4)
        self.assertEqual(_display_width("가A "), 4)

    def test_longer_distractors_remain_available_when_compact_pool_is_short(self):
        long_meaning = "자전거를 타고 멀리 여행하는 사람"
        bank = bank_fixture([
            ("u1", "sound", "소리", "ready"), ("u2", "apple", "사과", "ready"),
            ("u2", "tree", "나무", "ready"), ("u2", "traveler", long_meaning, "ready"),
            ("u2", "star", "별빛", "ready"),
        ])
        question = generate_script_questions([target(bank)], bank, seed=3)[0]
        self.assertEqual(wrong_texts(question), {"사과", "나무", "별빛", long_meaning})

    def test_compact_conflicts_allow_longer_nonconflicting_fallback(self):
        long_a = "자전거를 타고 멀리 여행하는 사람"
        long_b = "성공에 대한 확고한 믿음을 가지고 계속해서 일을 하다"
        bank = bank_fixture([
            ("u1", "sound", "소리", "ready"), ("u2", "apple", "사과", "ready"),
            ("u2", "big apple", "큰 사과", "ready"),
            ("u2", "star", "별빛", "ready"),
            ("u2", "traveler", long_a, "ready"), ("u2", "believe", long_b, "ready"),
        ])
        question = generate_script_questions([target(bank)], bank, seed=3)[0]
        self.assertTrue({long_a, long_b} <= wrong_texts(question))
        self.assertEqual(len(wrong_texts(question) & {"사과", "큰 사과"}), 1)

    def test_five_correct_choices_need_no_distractor_pool(self):
        bank = bank_fixture([('u1', 'sound', '소리, 음향 / 건전한; 해협\n울리다', 'ready')])
        question = generate_script_questions([target(bank)], bank, seed=1)[0]
        self.assertEqual(correct_texts(question), {'소리', '음향', '건전한', '해협', '울리다'})
        self.assertEqual(wrong_texts(question), set())
        self.assertEqual(question['choice_policy'], 'five_all')
        self.assertEqual(question['generation_evidence']['distractors'], [])
        validate_question(question)

    def test_confusable_word_meaning_is_included_even_when_longer_than_random_choices(self):
        meaning = '어떤 일이나 행동으로 생겨난 결과와 효과'
        bank = bank_fixture([
            ('u1', 'affect', '영향을 미치다', 'ready'),
            ('u2', 'effect', meaning, 'ready'),
            ('u2', 'apple', '사과', 'ready'), ('u2', 'tree', '나무', 'ready'),
            ('u2', 'river', '강', 'ready'), ('u2', 'star', '별빛', 'ready'),
        ])
        for seed in range(6):
            question = generate_script_questions([target(bank, 'affect')], bank, seed=seed)[0]
            self.assertIn(meaning, wrong_texts(question))
            evidence = next(item for item in question['generation_evidence']['distractors'] if item['text'] == meaning)
            self.assertEqual(evidence['source_words'], ['effect'])
            self.assertGreaterEqual(evidence['confusion_score'], 0.6)

    def test_confusable_word_never_overrides_known_meaning_filter(self):
        bank = bank_fixture([
            ('u1', 'affect', '영향을 미치다', 'ready'), ('u2', 'effect', '영향을 미치다', 'ready'),
            ('u2', 'apple', '사과', 'ready'), ('u2', 'tree', '나무', 'ready'),
            ('u2', 'river', '강', 'ready'), ('u2', 'star', '별빛', 'ready'),
        ])
        question = generate_script_questions([target(bank, 'affect')], bank, seed=3)[0]
        self.assertEqual(wrong_texts(question), {'사과', '나무', '강', '별빛'})

    def test_new_and_old_choice_policies_survive_candidate_switching(self):
        bank = bank_fixture()
        first = generate_script_questions([target(bank, 'apple')], bank, seed=1)[0]
        first['choice_policy'] = 'five_two'
        second = generate_script_questions([target(bank, 'apple')], bank, seed=9)[0]
        combined = add_candidate(first, second)
        for candidate in combined['candidates']:
            validate_question(select_candidate(combined, candidate['id']))

    def test_shared_source_row_excludes_a_synonym_of_a_correct_meaning(self):
        bank = bank_fixture([
            ('u1', 'hold up', '견디다; 떠받치다; 지연시키다', 'ready'),
            ('u2', 'uphold', '유지하다, 지탱하다', 'ready'),
            ('u2', 'sustain', '떠받치다, 지탱하다', 'ready'),
            ('u2', 'apple', '사과', 'ready'), ('u2', 'tree', '나무', 'ready'),
            ('u2', 'river', '강', 'ready'),
        ])
        for seed in range(4):
            q = generate_script_questions([target(bank, 'hold up')], bank, seed=seed)[0]
            self.assertNotIn('지탱하다', wrong_texts(q))
            self.assertNotIn('유지하다', wrong_texts(q))
            self.assertEqual(len(q['choices']), 5)

    def test_two_distractors_do_not_repeat_sibling_meanings_from_one_source_row(self):
        bank = bank_fixture([
            ('u1', 'fluctuate', '변동하다', 'ready'),
            ('u2', 'lucrative', '수익성이 좋은, 돈벌이가 되는', 'ready'),
            ('u2', 'apple', '사과', 'ready'), ('u2', 'tree', '나무', 'ready'),
            ('u2', 'river', '강', 'ready'), ('u2', 'star', '별빛', 'ready'),
        ])
        q = generate_script_questions([target(bank, 'fluctuate')], bank, seed=2)[0]
        self.assertEqual(len(wrong_texts(q) & {'수익성이 좋은', '돈벌이가 되는'}), 1)


if __name__ == "__main__":
    unittest.main()
