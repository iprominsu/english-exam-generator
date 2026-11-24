from copy import deepcopy
from html.parser import HTMLParser
from importlib.util import find_spec
import re
import unittest
from unittest.mock import Mock, patch
from urllib.parse import urlsplit
from xml.etree import ElementTree

from app import assets, vocab
from feature_word_meaning.registration_agent import materialize


class PageAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_head = False
        self.styles_outside_head = []
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "head":
            self.in_head = True
        if tag == "link" and attrs.get("rel") == "stylesheet" and not self.in_head:
            self.styles_outside_head.append(attrs["href"])
        self.urls.extend(attrs[key] for key in ("href", "src") if attrs.get(key))

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False


class AssetTests(unittest.TestCase):
    def test_local_assets_exist_and_pages_reference_the_current_build(self):
        for file in assets.FILES:
            self.assertTrue(file.is_file(), file.name)
        self.assertIn(f'<meta name="app-build" content="{assets.BUILD_ID}">', assets.index_html())
        for page in (assets.ROOT / "static").glob("*.html"):
            self.check_page(page)
        self.check_page(assets.ROOT / "feature_word_meaning/static/index.html")

    def check_page(self, page):
        with self.subTest(page=page.name):
            raw = page.read_text(encoding="utf-8")
            html = assets.index_html() if page == assets.ROOT / "static/index.html" else assets.versioned_html(raw)
            parsed = PageAssets()
            parsed.feed(html)
            self.assertFalse(parsed.styles_outside_head, parsed.styles_outside_head)
            self.assertNotIn("window.APP_CONFIG", html)
            for url in map(urlsplit, parsed.urls):
                if url.path.startswith("/static/"):
                    self.assertTrue((assets.ROOT / url.path[1:]).is_file(), url.path)
                if url.path in assets.STATIC_URLS:
                    self.assertEqual(url.query, "v=" + assets.BUILD_ID, url.path)

    def test_icon_registry_and_used_icons_match_the_sprite(self):
        static = assets.ROOT / "static"
        symbols = ElementTree.parse(static / "design-system/icons.svg").getroot()
        names = {symbol.attrib["id"].removeprefix("me-icon-") for symbol in symbols.findall("{*}symbol")}
        registry = (static / "design-system/icons.js").read_text(encoding="utf-8")
        declared = re.search(r"const names = Object\.freeze\(\[(.*?)\]\)", registry, re.S)
        self.assertIsNotNone(declared)
        self.assertSetEqual(names, set(re.findall(r"'([a-z-]+)'", declared[1])))
        for file in [*static.rglob("*.js"), *static.glob("*.html"), assets.ROOT / "feature_word_meaning/static/index.html"]:
            refs = re.findall(r"""(?:#me-icon-|(?:\bicon|MelodyIcons\.(?:create|svg))\(['"])([a-z][a-z-]*)""",
                              file.read_text(encoding="utf-8"))
            self.assertFalse(set(refs) - names, (file.name, set(refs) - names))


class GenerationTests(unittest.TestCase):
    def test_seven_types_and_new_candidate_use_the_selected_bank(self):
        from app import legacy_generator, validation

        words = "apple river tree cloud stone book house bird moon chair".split()
        meanings = "사과 강 나무 구름 돌 책 집 새 달 의자".split()
        bank = vocab.to_bank([{"단어": word, "뜻": meaning} for word, meaning in zip(words, meanings)], 1)
        examples = [
            ("The apple tasted sweet.", "We sliced the apple.", "사과는 달았다."),
            ("The river flooded the road.", "Fish live in the river.", "강물이 길을 덮었다."),
            ("The tree lost its leaves.", "We planted a tree.", "나무가 잎을 떨궜다."),
            ("A cloud covered the sun.", "Rain fell from the cloud.", "구름이 해를 가렸다."),
            ("The stone sank quickly.", "She lifted a stone.", "돌이 빠르게 가라앉았다."),
            ("I borrowed a book.", "The book has pictures.", "나는 책을 빌렸다."),
            ("They built a house.", "The house has three rooms.", "그들은 집을 지었다."),
            ("The bird spread its wings.", "We heard a bird singing.", "새가 날개를 펼쳤다."),
            ("The moon shone brightly.", "They landed on the moon.", "달이 밝게 빛났다."),
            ("She moved the chair.", "The chair has four legs.", "그녀가 의자를 옮겼다."),
        ]
        for entry, (first, second, translation) in zip(bank["entries"], examples, strict=True):
            source = {"origin": "source", "meaning": entry["meaning"]}
            entry["learning"] = {
                "examples": [{**source, "sentence": first, "translation": translation},
                             {**source, "sentence": second}],
                "passages": [{**source, "text": first + " " + second}],
            }
        original = deepcopy(bank)
        units = [bank["units"][0]["id"]]
        types = list(legacy_generator.TYPES)
        questions = legacy_generator.generate(bank, units, len(types), seed=31, question_types=types)
        self.assertEqual([q["type"] for q in questions], types)
        validation.validate_questions(questions, generated=True)
        self.assertEqual(questions, legacy_generator.generate(bank, units, len(types), seed=31, question_types=types))
        other = legacy_generator.regenerate(bank, units, questions[0], [questions[0]], request_id="candidate")
        self.assertEqual(other["type"], questions[0]["type"])
        self.assertNotEqual(other["payload"]["pairs"], questions[0]["payload"]["pairs"])
        self.assertEqual(bank, original)

    def test_saved_blank_preserves_literal_backslash(self):
        from app.legacy_generator import reuse_saved_material

        word = r"path\name"
        bank = vocab.to_bank([{"단어": word, "뜻": "경로"}], 2)
        reuse_saved_material(bank, {"type": "blank_ko", "answer": 1,
                                   "payload": {"choices": [word], "sentence": "Use ___ here."}})
        self.assertEqual(bank["entries"][0]["learning"]["examples"][0]["sentence"], f"Use {word} here.")

    def test_circled_source_number_keeps_entry_and_original_cell(self):
        items = [{"id": "label", "kind": "label", "text": "Day 1"}]
        for i, text in enumerate(("①", "apple", "사과")):
            cell = chr(65 + i) + "1"
            items.append({"id": cell, "kind": "cell", "cell": cell, "row": 1, "text": text})
        mapping = {"word_refs": ["B1"], "meaning_refs": ["C1"], "number_refs": ["A1"],
                   "unit_refs": ["label"], "unit_label": "Day 1", "related": []}
        region = {"id": "s1", "kind": "cells", "sheet": "Day 1", "items": items}
        plan = {"action": "map", "tables": [], "entries": [mapping], "metadata": [], "metadata_rows": []}
        entry, = materialize(region, plan, "fixture")["entries"]
        self.assertEqual((entry["source_number"], entry["word"], entry["meaning"]), (1, "apple", "사과"))
        self.assertEqual(entry["source"]["registration_refs"]["number_refs"], ["A1"])


@unittest.skipUnless(find_spec("fastapi") and find_spec("httpx"), "웹 통합 검사는 웹 의존성과 httpx가 필요합니다.")
class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        from app import main

        cls.main = main
        cls.client = TestClient(main.app)
        cls.addClassCleanup(cls.client.close)

    def test_auth_precedes_write_lock_and_asset_version_check(self):
        with patch.object(self.main.config, "APP_BASIC_PASS", "local-test"), \
             patch.object(self.main, "mutation_lock", Mock(locked=lambda: True)), \
             patch.object(self.main, "_invalidate_worksheet_cache") as invalidate:
            rejected = self.client.post("/api/generate", json={})
            self.assertEqual(rejected.status_code, 401)
            self.assertIn("no-store", rejected.headers["cache-control"])
            self.assertEqual(rejected.headers["x-content-type-options"], "nosniff")
            url = "/static/app.js?v=stale"
            self.assertEqual(self.client.get(url).status_code, 401)
            response = self.client.get(url, auth=(self.main.config.APP_BASIC_USER, "local-test"))
            self.assertEqual(response.status_code, 409)
            invalidate.assert_not_called()

    def test_public_routes_include_release_headers(self):
        with patch.object(self.main.config, "APP_BASIC_PASS", "local-test"):
            for path in ("/healthz", "/about", "/privacy"):
                with self.subTest(path=path):
                    response = self.client.get(path)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.headers["x-app-build"], assets.BUILD_ID)
                    self.assertEqual(response.headers["x-content-type-options"], "nosniff")

    def test_existing_source_link_dispatches_import(self):
        with patch.object(self.main.config, "APP_BASIC_PASS", ""), \
             patch.object(self.main.word_meaning_service, "import_existing", return_value={"bank_id": "b_old"}) as fetch:
            response = self.client.post("/api/word-meaning/imports/existing", json={"file_id": "old_file"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["bank_id"], "b_old")
        fetch.assert_called_once_with("old_file")

    def test_drive_errors_remain_distinguishable(self):
        from app.errors import public_error
        from feature_word_meaning.drive_sync import DriveSyncError

        for code, status in (("account_mismatch", 409), ("invalid_source", 422)):
            with self.subTest(code=code):
                error = public_error(DriveSyncError("연결을 확인하세요.", code))
                self.assertEqual(error.status_code, status)
                self.assertEqual((error.detail["code"], error.detail["retryable"]), (code, False))

    def test_sheet_rows_preserve_old_versions_and_candidate_columns(self):
        from app import sheets_io

        columns = ["no", "type", "instruction", "answer", "explanation", "payload_json", "set",
                   "source_file_id", "source_name", "exam_title", "request_id",
                   *[f"candidate_{i}_json" for i in range(1, 11)], "candidate_selected", "day_start",
                   "day_end", "deleted_at", "deletion_pending", "created_at", "updated_at"]
        self.assertEqual(sheets_io.SHEET_COLUMNS, columns)
        question = {"no": 1, "type": "blank_ko", "instruction": "빈칸을 채우세요.", "answer": 1,
                    "explanation": "", "source_file_id": "original", "day_end": None,
                    "payload": {"choices": ["tree", "river", "moon", "apple", "cloud"], "sentence": "A ___ grew here."}}
        question["alternatives"] = [{**question, "candidate_id": "original"}]
        row, = sheets_io.questions_to_rows([question])
        self.assertEqual(len(row), len(columns))
        record = dict(zip(columns, row, strict=True))
        self.assertEqual(record["day_end"], "")
        restored, = sheets_io.questions_from_records([record], exam_count=10)
        self.assertEqual((restored["source_file_id"], restored["payload"]), ("original", question["payload"]))
        self.assertEqual(restored["set"], 1)
        self.assertEqual(restored["alternatives"][0]["candidate_id"], "original")

    def test_only_paired_underline_tags_are_rendered(self):
        from app.render import _safe_instruction_html

        self.assertEqual(_safe_instruction_html("<u>A & B</u> / <u>C"), "<u>A &amp; B</u> / &lt;u&gt;C")
