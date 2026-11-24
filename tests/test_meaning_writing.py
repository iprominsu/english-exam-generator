"""Writing answers retain exact registered meanings without any generation AI."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from feature_word_meaning.exams import ExamError, confirm_question_review, question_content_hash, select_words, validate_question, validate_worksheet
from feature_word_meaning.meaning_writing import (
    WORKSHEET_KIND, create_writing_worksheet, edit_writing_question,
    generate_writing_questions, replace_writing_question,
)
from feature_word_meaning.drive_sync import _worksheet_tables


def bank_fixture(count=7):
    rows = [("u1", "sound", "[명] 소리, 음향"), ("u1", "apple", "[명] 사과"),
            ("u2", "sound", "[형] 건전한; 확실한"), ("u2", "look up", "[숙] 찾아보다"),
            ("u3", "sound", "[명] 해협"), ("u2", "US", "미국"), ("u2", "us", "우리를")]
    if count != 7:
        rows = [("u1", f"word {i}", f"[명] 뜻 {i}, 다른 뜻 ({i})") for i in range(count)]
    return {"id": "bank-writing", "schema_version": 1, "title": "원문 단어장",
            "source": {"filename": "원본.pdf", "format": "pdf", "sha256": "hash"},
            "units": [{"id": f"u{i}", "label": f"{i}강", "order": i} for i in range(1,4)],
            "entries": [{"id": f"e{i}", "unit_id": unit, "word": word, "meaning_raw": meaning,
                         "meaning": meaning, "status": "ready"} for i,(unit,word,meaning) in enumerate(rows)]}


def worksheet_fixture(bank=None, count=None, units=None, seed=11):
    bank = bank or bank_fixture()
    units = units or ["u1", "u2"]
    words = select_words(bank, units, count=count, shuffle=True, seed=seed)
    questions = generate_writing_questions(words, bank)
    return create_writing_worksheet(bank, units, questions, selected_words=words)


class WritingCoreTests(unittest.TestCase):
    def test_all_registered_meanings_pos_and_original_corrections_preserved(self):
        bank = bank_fixture()
        bank["entries"][0]["meaning_raw"] = "잘못된 OCR 원문"
        before = deepcopy(bank)
        worksheet = worksheet_fixture(bank)
        sound = next(q for q in worksheet["questions"] if q["word"] == "sound")
        self.assertEqual(sound["answer_text"], "[명] 소리, 음향\n[형] 건전한; 확실한")
        self.assertNotIn("해협", sound["answer_text"])
        self.assertIn("잘못된 OCR 원문", sound["original_meaning_raw"])
        self.assertEqual(worksheet["worksheet_kind"], WORKSHEET_KIND)
        self.assertEqual(bank, before)
        for q in worksheet["questions"]:
            self.assertNotIn("choices", q)
            validate_question(q)
        validate_worksheet(worksheet)

    def test_random_selection_and_order_are_reproducible_for_boundaries(self):
        bank = bank_fixture(65)
        for count in (1,29,30,31,60,None):
            with self.subTest(count=count):
                a = worksheet_fixture(bank,count,["u1"])
                b = worksheet_fixture(bank,count,["u1"])
                self.assertEqual(a["questions"],b["questions"])
                self.assertEqual(len(a["questions"]),count or 65)
                self.assertEqual(len({q["word"] for q in a["questions"]}),count or 65)
                if count is None:
                    self.assertNotEqual([q["word"] for q in a["questions"]],[e["word"] for e in bank["entries"]])
        with self.assertRaises(ExamError):
            worksheet_fixture(bank,66,["u1"])

    def test_exact_case_and_whitespace_rules_match_shared_selection(self):
        bank=bank_fixture()
        bank["entries"].append(dict(bank["entries"][0],id="e7",word=" sound "))
        worksheet=worksheet_fixture(bank)
        self.assertEqual(len(worksheet["questions"]),5)
        sound=next(q for q in worksheet["questions"] if q["word"]=="sound")
        self.assertEqual(sound["answer_text"].count("소리"),1)
        self.assertEqual(len(sound["source_entry_ids"]),3)
        self.assertEqual(len({q["word"] for q in worksheet["questions"] if q["word"] in {"US","us"}}),2)

    def test_edit_changes_only_display_and_invalidates_review_hash(self):
        worksheet=worksheet_fixture()
        old=worksheet["questions"][0]
        reviewed=confirm_question_review(old)
        updated=edit_writing_question(reviewed,word="edited word",answer_text="사람이 수정한 정답")
        self.assertTrue(updated["word_edited"])
        self.assertTrue(updated["answer_edited"])
        self.assertNotEqual(question_content_hash(reviewed),question_content_hash(updated))
        self.assertEqual(updated["review_status"],"needs_review")
        self.assertEqual(updated["source_snapshot"],old["source_snapshot"])
        self.assertEqual(updated["original_answer_text"],old["answer_text"])
        self.assertEqual(updated["source_word"],old["word"])
        validate_question(updated)
        with self.assertRaises(ExamError):
            validate_question(updated,require_reviewed=True)
        validate_question(confirm_question_review(updated),require_reviewed=True)
        self.assertEqual(edit_writing_question(reviewed),reviewed)

    def test_bad_answers_and_source_tampering_are_rejected(self):
        q=worksheet_fixture()["questions"][0]
        for word,answer in (("",None),(None,""),("x"*301,None)):
            with self.subTest(word=word,answer=answer),self.assertRaises(ExamError):
                edit_writing_question(q,word=word,answer_text=answer)
        for change in ({"original_answer_text":"조작"},{"source_word":"other"},{"meaning_raw":"조작"},{"answer_edited":True},{"choices":[]}):
            bad=deepcopy(q);bad.update(change)
            with self.subTest(change=change),self.assertRaises(ExamError):
                validate_question(bad)
        bad=deepcopy(q);bad["source_snapshot"]["source_entries"][0]["meaning"]="조작"
        with self.assertRaises(ExamError):validate_question(bad)

    def test_scope_and_source_snapshots_are_frozen(self):
        bank=bank_fixture()
        words=select_words(bank,["u1"],shuffle=True,seed=2)
        q=generate_writing_questions(words,bank)
        bank["entries"][0]["meaning"]="changed after selection"
        with self.assertRaises(ExamError):create_writing_worksheet(bank,["u1"],q,selected_words=words)
        worksheet=worksheet_fixture()
        bad=deepcopy(worksheet);bad["source_entries"][0]["meaning"]="조작"
        with self.assertRaises(ExamError):validate_worksheet(bad)
        bad=deepcopy(worksheet);bad["selected_unit_ids"]=["u3"]
        with self.assertRaises(ExamError):validate_worksheet(bad)

    def test_source_review_blocks_until_explicit_partial(self):
        bank=bank_fixture();bank["entries"][1]["status"]="review"
        with self.assertRaises(ExamError):worksheet_fixture(bank)
        words=select_words(bank,["u1","u2"],shuffle=True,allow_partial=True,seed=1)
        worksheet=create_writing_worksheet(bank,["u1","u2"],generate_writing_questions(words,bank),selected_words=words,allow_partial=True)
        self.assertNotIn("apple",[q["word"] for q in worksheet["questions"]])

    def test_replace_uses_unused_frozen_scope_and_retains_position_id(self):
        bank=bank_fixture(8)
        worksheet=worksheet_fixture(bank,3,["u1"])
        before=deepcopy(worksheet);before_bank=deepcopy(bank)
        bank["entries"][4]["meaning"]="later database change"
        target=worksheet["questions"][1]["id"]
        updated=replace_writing_question(worksheet,target,bank,seed=1)
        old_ids={q["source_word_id"] for q in worksheet["questions"]}
        self.assertEqual(updated["questions"][1]["id"],target)
        self.assertNotIn(updated["questions"][1]["source_word_id"],old_ids)
        self.assertEqual(updated["questions"][0],worksheet["questions"][0])
        self.assertEqual(updated["questions"][2],worksheet["questions"][2])
        self.assertEqual(updated["generation_bank_snapshot"],before_bank)
        self.assertTrue(updated["pdfs_stale"])
        validate_worksheet(updated)
        self.assertEqual(worksheet,before)
        self.assertEqual(replace_writing_question(worksheet,target,bank,seed=1),updated)

    def test_replace_rejects_full_scope_and_unknown_question(self):
        worksheet=worksheet_fixture()
        with self.assertRaises(ExamError) as caught:
            replace_writing_question(worksheet,worksheet["questions"][0]["id"],seed=1)
        self.assertEqual(caught.exception.code,"no_replacement_available")
        with self.assertRaises(ExamError) as caught:replace_writing_question(worksheet,"missing")
        self.assertEqual(caught.exception.code,"question_not_found")

    def test_replace_excludes_manual_displayed_word_as_well_as_source_ids(self):
        bank=bank_fixture(4);worksheet=worksheet_fixture(bank,2,["u1"])
        free=[e["word"] for e in bank["entries"] if e["word"] not in {q["word"] for q in worksheet["questions"]}]
        worksheet["questions"][0]=edit_writing_question(worksheet["questions"][0],word=free[0])
        target=worksheet["questions"][1]["id"]
        updated=replace_writing_question(worksheet,target,seed=1)
        self.assertEqual(updated["questions"][1]["word"],free[1])
        validate_worksheet(updated,require_reviewed=False)

    def test_drive_tables_have_editable_answers_and_original_source_without_choices(self):
        worksheet=worksheet_fixture()
        worksheet["questions"][0]=edit_writing_question(worksheet["questions"][0],answer_text="=수동 정답")
        tables=_worksheet_tables(worksheet,require_reviewed=False)
        self.assertEqual(set(tables),{"Questions","SourceEntries","Metadata"})
        head=tables["Questions"][0];row=tables["Questions"][1]
        self.assertEqual(row[head.index("answer_text")],"=수동 정답")
        self.assertEqual(row[head.index("original_answer_text")],worksheet["questions"][0]["original_answer_text"])
        self.assertNotIn("choices_json",head)
        self.assertIn(["worksheet_kind",WORKSHEET_KIND],tables["Metadata"])


if __name__=="__main__":unittest.main()
