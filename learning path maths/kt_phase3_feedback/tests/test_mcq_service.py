"""Tests for the session-aware MCQ service boundary."""
import json
import tempfile
import unittest
from pathlib import Path

import phase3_paths  # noqa: F401 -- installs the Phase 2 import path
from mcq_service import MCQSessionService
from session_store import SessionStore
from tests.helpers import make_bank, responses


class MCQSessionServiceTests(unittest.TestCase):
    def service(self, root):
        return MCQSessionService(make_bank(), SessionStore(Path(root)))

    def test_start_serves_only_public_half_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = self.service(tmp)
            result = service.start_session()
            self.assertEqual(len(result["questions"]), 20)
            blob = json.dumps(result)
            for private in ("answer_index", "content_text", "item_id",
                            "exercise_id", "correct"):
                self.assertNotIn(private, blob)

    def test_submit_halves_and_checkpoint_feeds(self):
        bank = make_bank()
        with tempfile.TemporaryDirectory() as tmp:
            service = MCQSessionService(bank, SessionStore(Path(tmp)))
            session = service.start_session()
            sid = session["session_id"]
            with self.assertRaises(ValueError):
                service.questions(sid, 2)
            mid = service.submit_half(sid, 1, responses(bank, count=20))
            self.assertEqual(mid["checkpoint"], "midpoint")
            self.assertEqual(mid["feed"]["teacher"]["total"],
                             {"correct": 20, "out_of": 20})
            second = service.questions(sid, 2)
            self.assertEqual(len(second["questions"]), 20)
            end = service.submit_half(sid, 2, responses(bank, False, 40)[20:])
            self.assertEqual(end["checkpoint"], "end")
            self.assertEqual(end["feed"]["teacher"]["total"],
                             {"correct": 20, "out_of": 40})
            self.assertEqual(service.snapshot(sid)["status"], "complete")
            with self.assertRaises(ValueError):
                service.submit_response(sid, responses(bank)[0])

    def test_submit_response_is_ordered_and_private(self):
        bank = make_bank()
        with tempfile.TemporaryDirectory() as tmp:
            service = MCQSessionService(bank, SessionStore(Path(tmp)))
            sid = service.start_session()["session_id"]
            bad = {"question_id": bank["questions"][1]["question_id"],
                   "selected_index": 0}
            with self.assertRaisesRegex(ValueError, "required order"):
                service.submit_response(sid, bad)
            result = service.submit_response(sid, responses(bank)[0])
            self.assertEqual(result["position"], 1)
            self.assertIsNone(result["feed"])
            session = service.store.load(sid)
            self.assertTrue(session["responses"][0]["correct"])
            self.assertEqual(session["responses"][0]["selected_text"],
                             bank["questions"][0]["options"][0])

    def test_session_rejects_changed_bank(self):
        bank = make_bank()
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            service = MCQSessionService(bank, store)
            sid = service.start_session()["session_id"]
            changed = make_bank()
            changed["questions"][0]["answer_index"] = 1
            other = MCQSessionService(changed, store)
            with self.assertRaisesRegex(ValueError, "different bank"):
                other.snapshot(sid)


if __name__ == "__main__":
    unittest.main()
