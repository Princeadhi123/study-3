"""Optional local contract test for the real approved Phase 2 bank.

This test uses only the JSON bank and a temporary session store. It does not
load the frozen checkpoint or the multi-GB embedding table, and it skips when
the private approved artifact is unavailable.
"""
import json
import tempfile
import unittest
from pathlib import Path

import phase3_paths
from mcq_service import MCQSessionService
from session_store import SessionStore


@unittest.skipUnless(
    phase3_paths.APPROVED_BANK.exists(),
    "approved v2 bank is a private local artifact",
)
class RealBankContractTests(unittest.TestCase):
    def service(self, tmpdir: str) -> MCQSessionService:
        return MCQSessionService.from_default(SessionStore(Path(tmpdir)))

    def test_start_session_serves_only_public_half_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = self.service(tmpdir)
            start = service.start_session()
            self.assertEqual(20, len(start["questions"]))
            for question in start["questions"]:
                self.assertEqual(
                    {"question_id", "skill_id", "text", "options"},
                    set(question),
                )

    def test_taxonomy_covers_real_bank_and_student_scores_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = self.service(tmpdir)
            self.assertIsNotNone(service.taxonomy)
            self.assertEqual(15, sum(len(topic["subtopics"])
                                     for topic in service.taxonomy["topics"]))
            sid = service.start_session()["session_id"]
            rows = [{"question_id": question["question_id"],
                     "selected_index": question["answer_index"]}
                    for question in service.bank["questions"]]
            midpoint = service.submit_half(sid, 1, rows[:20])["feed"]["student"]
            end = service.submit_half(sid, 2, rows[20:])["feed"]["student"]
            self.assertEqual((20, 20), (sum(r["correct"] for r in midpoint["subtopics"]),
                                        sum(r["out_of"] for r in midpoint["subtopics"])))
            self.assertEqual((40, 40), (sum(r["correct"] for r in end["subtopics"]),
                                        sum(r["out_of"] for r in end["subtopics"])))
            self.assertEqual(15, len(end["subtopics"]))
            self.assertTrue(all(set(row) == {"skill_id", "skill_name", "subtopic_id",
                                             "subtopic_name", "correct", "out_of"}
                                for row in end["subtopics"]))

    def test_approved_bank_scores_two_ordered_halves_privately(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = self.service(tmpdir)
            session_id = service.start_session()["session_id"]
            rows = [{
                "question_id": question["question_id"],
                "selected_index": question["answer_index"],
            } for question in service.bank["questions"]]
            midpoint = service.submit_half(session_id, 1, rows[:20])
            end = service.submit_half(session_id, 2, rows[20:])
            self.assertEqual("midpoint", midpoint["checkpoint"])
            self.assertEqual(20, midpoint["feed"]["teacher"]["total"]["correct"])
            self.assertEqual("end", end["checkpoint"])
            self.assertEqual(40, end["feed"]["teacher"]["total"]["correct"])

    def test_full_bank_boundary_profiles_and_private_checkpoint_contract(self):
        questions = self.service_bank_questions()
        cases = {
            "all_correct": [True] * 40,
            "all_incorrect": [False] * 40,
            "first_half_only": [True] * 20 + [False] * 20,
            "second_half_only": [False] * 20 + [True] * 20,
        }
        for name, outcomes in cases.items():
            with self.subTest(profile=name), tempfile.TemporaryDirectory() as tmpdir:
                service = self.service(tmpdir)
                start = service.start_session()
                sid = start["session_id"]
                self.assertEqual([q["question_id"] for q in questions[:20]],
                                 [q["question_id"] for q in start["questions"]])
                with self.assertRaises(ValueError):
                    service.questions(sid, 2)
                with self.assertRaises(ValueError):
                    service.submit_response(sid, {"question_id": questions[1]["question_id"],
                                                  "selected_index": 0})
                rows = [{"question_id": q["question_id"],
                         "selected_index": q["answer_index"] if correct else
                         (q["answer_index"] + 1) % len(q["options"])}
                        for q, correct in zip(questions, outcomes)]
                for half, limit in ((1, 20), (2, 40)):
                    if half == 2:
                        served = service.questions(sid, 2)["questions"]
                        self.assertEqual([q["question_id"] for q in questions[20:]],
                                         [q["question_id"] for q in served])
                    for position in range(limit - 20, limit):
                        response = service.submit_response(sid, rows[position])
                        self.assertEqual(position + 1, response["position"])
                        self.assertEqual("midpoint" if position == 19 else
                                         "end" if position == 39 else None,
                                         response["checkpoint"])
                        if position not in (19, 39):
                            self.assertIsNone(response["feed"])
                    feed = response["feed"]
                    expected = sum(outcomes[:limit])
                    self.assertEqual({"correct": expected, "out_of": limit},
                                     feed["teacher"]["total"])
                    self.assertEqual(limit, sum(s["out_of"] for s in
                                                feed["student"]["skills"]))
                    self.assertEqual(expected, sum(s["correct"] for s in
                                                   feed["student"]["skills"]))
                    self.assertEqual(limit, sum(s["out_of"] for s in
                                                feed["student"]["subtopics"]))
                    for skill in feed["student"]["skills"]:
                        positions = [i for i, q in enumerate(questions[:limit])
                                     if q["skill_id"] == skill["skill_id"]]
                        self.assertEqual((sum(outcomes[i] for i in positions),
                                          len(positions)),
                                         (skill["correct"], skill["out_of"]))
                    self.assertEqual("deterministic", feed["student"]["message_source"])
                    if half == 1:
                        self.assertNotIn("total", feed["student"])
                        self.assertEqual("You have completed 20 questions. You are halfway "
                                         "through the assessment. Continue when you are ready.",
                                         feed["student"]["message"])
                    else:
                        self.assertEqual({"correct": expected, "out_of": 40},
                                         feed["student"]["total"])
                        self.assertEqual(
                            "All 40 questions answered. These results describe "
                            "your answers on this assessment, not your overall mastery.",
                            feed["student"]["summary"])
                        self.assertTrue(feed["student"]["message"].startswith(
                            "You have completed all 40 questions."))
                    public = json.dumps(feed["student"])
                    for private in ("answer_index", "selected_index", "question_id",
                                    "selected_text", "conformal", "model_estimate",
                                    "routing", "graph", "teacher"):
                        self.assertNotIn(private, public)
                    self.assertEqual(limit, service.snapshot(sid)["answered_count"])
                record = service.private_record(sid)
                self.assertEqual("complete", record["status"])
                self.assertEqual([q["question_id"] for q in questions],
                                 [r["question_id"] for r in record["responses"]])
                self.assertEqual(outcomes, [r["correct"] for r in record["responses"]])
                with self.assertRaises(ValueError):
                    service.submit_response(sid, rows[0])

    def service_bank_questions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            return self.service(tmpdir).bank["questions"]


if __name__ == "__main__":
    unittest.main()
