"""Legacy workbook provenance and login refreshes, using in-memory fixtures."""

from importlib.util import find_spec
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import vocab


@unittest.skipUnless(find_spec("openpyxl"), "Excel tests require openpyxl")
class LegacyWorkbookTests(unittest.TestCase):
    def workbook(self, second_meaning="강"):
        from openpyxl import Workbook

        source = BytesIO()
        book = Workbook()
        first = book.active
        first.title = "Day 01"
        first.append(["단어", "뜻", "뜻"])
        first.append(["apple", "사과", "뒤쪽 중복 열"])
        first.append([])
        first.append(["tree", "나무"])
        second = book.create_sheet("Day 2")
        second.append(["단어", "뜻"])
        second.append(["river", second_meaning])
        book.save(source)
        book.close()
        source.seek(0)
        return source

    def test_range_interleaves_chapters_and_preserves_physical_rows(self):
        with self.workbook() as source:
            self.assertEqual(vocab.available_days(source), [1, 2])
            rows = vocab.load_range(source, 1, 2)
            single = vocab.load_range(source, 1)
        bank = vocab.to_bank(rows, 1)
        self.assertEqual([entry["word"] for entry in bank["entries"]], ["apple", "river", "tree"])
        self.assertEqual(bank["entries"][0]["meaning"], "사과")
        self.assertEqual([entry["source"] for entry in bank["entries"]], [
            {"sheet": "Day 01", "row": 2}, {"sheet": "Day 2", "row": 2}, {"sheet": "Day 01", "row": 4},
        ])
        self.assertEqual([row["단어"] for row in single], ["apple", "tree"])

    def test_range_rejects_incomplete_day(self):
        with self.workbook(None) as source:
            with self.assertRaisesRegex(ValueError, "DAY 2"):
                vocab.load_range(source, 1, 2)


@unittest.skipUnless(find_spec("gspread"), "Route tests require the web dependencies")
class CheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_generated_rows_can_resume_after_original_xlsx_is_moved(self):
        from app import main

        request_id = "existing-worksheet-request"
        body = main.GenerateBody(request_id=request_id, day=2, vocab_file_id="source-file", count=1)
        record = {
            "request_id": request_id, "state": "generated",
            "params": {"day": 2, "vocab_file_id": "source-file", "count": 1, "provider": "script"},
            "questions": [{"source_file_id": "source-file", "source_name": "book.xlsx"}],
        }
        render = AsyncMock(return_value={"version": 3})
        with (patch.object(main.operations, "load", return_value=record),
              patch.object(main.operations, "save") as save,
              patch.object(main, "_vocab_meta", side_effect=AssertionError("unexpected Drive read")),
              patch.object(main, "_download_vocab", side_effect=AssertionError("unexpected download")),
              patch.object(main.pipeline, "generate_and_render", render)):
            result = await main.generate(body)
        self.assertEqual(result, {"request_id": request_id, "version": 3})
        self.assertEqual(render.await_args.args, (2, None, 1))
        self.assertIs(render.await_args.kwargs["record"], record)
        self.assertEqual(record["state"], "done")
        save.assert_called_once_with(record)


@unittest.skipUnless(find_spec("dotenv"), "Subscription tests require python-dotenv")
class CredentialRefreshTests(unittest.TestCase):
    original = b'{"auth_mode":"chatgpt","tokens":{"access_token":"fixture-old"}}'
    refreshed = b'{"auth_mode":"chatgpt","tokens":{"access_token":"fixture-new"}}'

    def run_session(self, contents, *, body_error=None, events=None):
        from app import gpt_subscription as gpt

        events = events if events is not None else []
        store = Mock()
        store.load.return_value = self.original
        store.save.side_effect = lambda value: events.append(("save", value))

        def new_client(home, workspace, **kwargs):
            (home / "auth.json").write_bytes(contents)
            return Mock()

        with TemporaryDirectory() as directory, patch.object(gpt, "CodexSession", new_client):
            with gpt.session(store=store, temp_root=Path(directory),
                             validate_auth=lambda value: events.append(("validate", value)),
                             before_persist=lambda: events.append(("ownership", None))):
                if body_error is not None:
                    raise body_error

    def test_valid_refresh_checks_identity_and_ownership_before_writing(self):
        events = []
        self.run_session(self.refreshed, events=events)
        self.assertEqual(events, [("validate", self.refreshed), ("ownership", None), ("save", self.refreshed)])

    def test_incomplete_refresh_does_not_overwrite_last_valid_login(self):
        from app.gpt_subscription import SubscriptionError

        for invalid in [b"{", b"{}", b'{"auth_mode":"chatgpt","tokens":{}}']:
            events = []
            with self.subTest(invalid=invalid), self.assertRaises(SubscriptionError):
                self.run_session(invalid, events=events)
            self.assertEqual(events, [])

    def test_cleanup_failure_preserves_primary_error(self):
        events = []
        with self.assertLogs(level="WARNING"), self.assertRaisesRegex(RuntimeError, "original failure"):
            self.run_session(b"{}", body_error=RuntimeError("original failure"), events=events)
        self.assertEqual(events, [])
