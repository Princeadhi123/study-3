"""Optional local contract test for the real approved Phase 2 bank.

This test uses only the JSON bank and a temporary session store. It does not
load the frozen checkpoint or the multi-GB embedding table, and it skips when
the private approved artifact is unavailable.
"""
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


if __name__ == "__main__":
    unittest.main()
