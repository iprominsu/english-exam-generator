import hashlib
import os
import tempfile
import unittest
from importlib.util import find_spec
from pathlib import Path
from unittest.mock import Mock, patch


class WorkerRegressions(unittest.TestCase):
    @unittest.skipUnless(find_spec("fastapi"), "FastAPI is required")
    def test_links_added_during_source_preparation_survive_publication(self):
        from fastapi import HTTPException
        from feature_word_meaning.api import FeatureService
        from feature_word_meaning.import_cloud import install_bank_write_guard
        from feature_word_meaning.import_engine import MemoryStateStore
        from feature_word_meaning.import_pipeline import DurableImportCoordinator
        from feature_word_meaning.source_integrity import freeze_source_manifest

        content = b"%PDF-test"
        sha = hashlib.sha256(content).hexdigest()
        source_bank = freeze_source_manifest({
            "source": {"sha256": sha, "format": "pdf"},
            "units": [{"id": "u1", "label": "1강", "order": 0, "pages": [1]}],
            "entries": [{"id": "e1", "unit_id": "u1", "status": "ready", "word": "map",
                         "meaning": "지도", "source": {"page": 1, "row": 1}}],
        })
        with tempfile.TemporaryDirectory() as directory:
            web, worker = FeatureService(directory), FeatureService(directory)
            self.addCleanup(web.executor.shutdown)
            self.addCleanup(worker.executor.shutdown)
            state = MemoryStateStore()
            coordinator = DurableImportCoordinator(worker, state, Mock(), structured_provider=Mock(), account_key="test")
            for service in (web, worker):
                install_bank_write_guard(service, state)
            web.import_coordinator = coordinator
            web.new_import_fields = coordinator.new_fields
            web.dispatch = coordinator.enqueue_prepare

            def link(file_id):
                return web.register_upload("source.pdf", content, legacy_original_id=file_id)

            job_id = link("first")["job_id"]

            def parse(*args, **kwargs):
                stale_job = worker.store.get("jobs", job_id)
                self.assertEqual(link("during-read")["job_id"], job_id)
                worker.store.put("jobs", stale_job)
                return {"bank": source_bank}

            put = worker.store.put_metadata

            def publish(kind, bank, **kwargs):
                if kind == "banks" and kwargs.get("expected_version") == 0:
                    with self.assertRaises(HTTPException) as busy:
                        link("during-write")
                    self.assertEqual(busy.exception.status_code, 409)
                return put(kind, bank, **kwargs)

            with patch("feature_word_meaning.registration_agent.step", side_effect=parse), \
                    patch.object(worker.store, "put_metadata", side_effect=publish):
                coordinator.prepare({"job_id": job_id})
            bank_id = "b_" + sha[:24]
            self.assertEqual(link("during-write")["bank_id"], bank_id)
            self.assertEqual(link("during-read")["bank_id"], bank_id)
            self.assertEqual(web.store.get("banks", bank_id)["source"]["legacy_original_ids"],
                             ["first", "during-read", "during-write"])

    @unittest.skipUnless(find_spec("fastapi"), "FastAPI is required")
    def test_removed_regeneration_target_preserves_the_worksheet(self):
        from feature_word_meaning.production import ProductionFeatureService
        from feature_word_meaning.storage import ConflictError

        service = Mock()
        service.get.return_value = {"id": "w1", "version": 3, "questions": []}
        job = {
            "id": "j1", "kind": "regenerate", "completed_batches": {"0": [{}]},
            "payload": {"worksheet_id": "w1", "question_id": "q1", "version": 3},
        }
        with self.assertRaises(ConflictError):
            ProductionFeatureService._finish(service, job)
        service.store.put.assert_not_called()

    @unittest.skipUnless(find_spec("reportlab"), "ReportLab is required")
    def test_failed_answer_publication_restores_both_existing_pdfs(self):
        from feature_word_meaning.render import _stage_pdf_pair

        with tempfile.TemporaryDirectory() as directory:
            student = Path(directory) / "student.pdf"
            answer = Path(directory) / "answer.pdf"
            student.write_bytes(b"old student")
            answer.write_bytes(b"old answer")
            replace = os.replace

            def fail_answer(source, destination):
                if destination == answer:
                    raise OSError("answer file busy")
                return replace(source, destination)

            with patch("feature_word_meaning.render.os.replace", side_effect=fail_answer):
                with self.assertRaisesRegex(OSError, "answer file busy"):
                    with _stage_pdf_pair(student, answer) as (new_student, new_answer):
                        new_student.write_bytes(b"new student")
                        new_answer.write_bytes(b"new answer")
            self.assertEqual(student.read_bytes(), b"old student")
            self.assertEqual(answer.read_bytes(), b"old answer")
            self.assertEqual(set(Path(directory).iterdir()), {student, answer})
