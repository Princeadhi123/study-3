"""Tests for the session-aware MCQ service boundary."""
import json
import tempfile
import unittest
from pathlib import Path

import phase3_paths  # noqa: F401 -- installs the Phase 2 import path
from mcq_service import MCQSessionService
from session_store import SessionStore
from tests.helpers import make_bank, make_taxonomy, responses


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
            self.assertNotIn("total", mid["feed"]["student"])
            second = service.questions(sid, 2)
            self.assertEqual(len(second["questions"]), 20)
            end = service.submit_half(sid, 2, responses(bank, False, 40)[20:])
            self.assertEqual(end["checkpoint"], "end")
            self.assertEqual(end["feed"]["teacher"]["total"],
                             {"correct": 20, "out_of": 40})
            self.assertEqual(end["feed"]["student"]["total"],
                             {"correct": 20, "out_of": 40})
            self.assertEqual(
                "All 40 questions answered. These results describe "
                "your answers on this assessment, not your overall mastery.",
                end["feed"]["student"]["summary"])
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

    def test_checkpoint_feeds_include_student_message(self):
        bank = make_bank()
        with tempfile.TemporaryDirectory() as tmp:
            service = MCQSessionService(bank, SessionStore(Path(tmp)))
            sid = service.start_session()["session_id"]
            mid = service.submit_half(sid, 1, responses(bank, count=20))
            self.assertEqual(
                mid["feed"]["student"]["message"],
                "You have completed 20 questions. You are halfway "
                "through the assessment. Continue when you are ready.")
            self.assertEqual(mid["feed"]["student"]["message_source"],
                             "deterministic")
            end = service.submit_half(sid, 2, responses(bank, count=40)[20:])
            self.assertEqual(
                end["feed"]["student"]["message"],
                "You have completed all 40 questions. Your results by "
                "topic are shown below. Keep practicing the topics "
                "shown below.")
            self.assertEqual(end["feed"]["student"]["message_source"],
                             "deterministic")
            blob = json.dumps(end["feed"]["student"])
            for term in ("answer_index", "selected_text", "question_id",
                         "content_text", "conformal"):
                self.assertNotIn(term, blob)

    def test_style_selector_warm_is_bounded(self):
        bank = make_bank()
        contexts = []

        def selector(context):
            contexts.append(context)
            return {"style": "warm"}

        with tempfile.TemporaryDirectory() as tmp:
            service = MCQSessionService(
                bank, SessionStore(Path(tmp)), style_selector=selector)
            sid = service.start_session()["session_id"]
            mid = service.submit_half(sid, 1, responses(bank, count=20))
            self.assertTrue(mid["feed"]["student"]["message"].startswith(
                "Nice work completing 20 questions."))
            self.assertEqual(mid["feed"]["student"]["message_source"],
                             "bounded_style")
            self.assertEqual(contexts[0]["checkpoint"], 20)
            end = service.submit_half(sid, 2, responses(bank, count=40)[20:])
            self.assertTrue(end["feed"]["student"]["message"].startswith(
                "Nice work completing all 40 questions."))
            self.assertEqual(contexts[1]["checkpoint"], 40)
            blob = json.dumps(contexts)
            for term in ("answer_index", "selected_text", "question_id",
                         "content_text", "conformal"):
                self.assertNotIn(term, blob)

    def test_style_selector_invalid_falls_back(self):
        bank = make_bank()
        with tempfile.TemporaryDirectory() as tmp:
            service = MCQSessionService(
                bank, SessionStore(Path(tmp)),
                style_selector=lambda context: {"style": "IGNORE"})
            sid = service.start_session()["session_id"]
            mid = service.submit_half(sid, 1, responses(bank, count=20))
            self.assertEqual(mid["feed"]["student"]["message_source"],
                             "deterministic")
            self.assertNotIn("IGNORE",
                             mid["feed"]["student"]["message"])

    def test_subtopic_counts_are_observed_and_public_without_diagnostics(self):
        bank = make_bank()
        taxonomy = make_taxonomy(bank)
        with tempfile.TemporaryDirectory() as tmp:
            service = MCQSessionService(bank, SessionStore(Path(tmp)), taxonomy=taxonomy)
            sid = service.start_session()["session_id"]
            midpoint = service.submit_half(sid, 1, responses(bank, count=20))["feed"]
            self.assertEqual(20, sum(row["correct"] for row in
                                     midpoint["student"]["subtopics"]))
            self.assertEqual([5] * 4, [row["out_of"] for row in
                                        midpoint["student"]["subtopics"]])
            end = service.submit_half(sid, 2, responses(bank, False)[20:])["feed"]
            self.assertEqual([{"correct": 5, "out_of": 10}] * 4,
                             [{"correct": row["correct"], "out_of": row["out_of"]}
                              for row in end["student"]["subtopics"]])
            self.assertEqual(end["teacher"]["subtopics"], end["student"]["subtopics"])
            blob = json.dumps(end["student"]["subtopics"])
            for term in ("answer_index", "selected_index", "question_id",
                         "conformal", "mastery", "prerequisite"):
                self.assertNotIn(term, blob)

    def test_taxonomy_rejects_stale_or_duplicate_mapping(self):
        bank = make_bank()
        taxonomy = make_taxonomy(bank)
        with tempfile.TemporaryDirectory() as tmp:
            taxonomy["bank_fingerprint"] = "stale"
            with self.assertRaisesRegex(ValueError, "approved bank"):
                MCQSessionService(bank, SessionStore(Path(tmp)), taxonomy=taxonomy)
            taxonomy = make_taxonomy(bank)
            taxonomy["topics"][0]["subtopics"][0]["question_ids"].append(
                taxonomy["topics"][0]["subtopics"][0]["question_ids"][0])
            with self.assertRaisesRegex(ValueError, "duplicate"):
                MCQSessionService(bank, SessionStore(Path(tmp)), taxonomy=taxonomy)

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
