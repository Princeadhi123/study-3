"""Checks deterministic MCQ review flags without treating them as approval."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from review_mcq_inventory import audit, review_flags


class ReviewTests(unittest.TestCase):
    def test_flags_are_review_leads_only(self):
        q = {"text": "Kuinka monta astetta kulma x on?", "options": ["20°", "40°"],
             "skill_name": "KLIKKAA TÄSTÄ"}
        self.assertEqual(set(review_flags(q)), {
            "possible_missing_visual_or_external_context", "underspecified_prompt_heuristic",
            "two_choice_guessing_risk", "nonskill_label"})
        self.assertEqual(review_flags({"text": "What is 20 plus 30 expressed as a number?",
                                       "options": ["40", "50", "60"],
                                       "skill_name": "Arithmetic"}), [])

    def test_detects_conflicting_visible_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, flags, report_path = (root / "inventory.gz", root / "flags.gz",
                                          root / "report.json")
            rows = [
                {"question_id": "a", "skill_id": "s1", "text": "Add the numbers", 
                 "options": ["1", "2", "3"], "answer_index": 0, "skill_name": "Math"},
                {"question_id": "b", "skill_id": "s2", "text": "Add  the numbers",
                 "options": ["3", "2", "1"], "answer_index": 1, "skill_name": "Math"},
            ]
            with gzip.open(source, "wt", encoding="utf-8") as stream:
                for row in rows:
                    stream.write(json.dumps(row) + "\n")
            report = audit(source, flags, report_path)
            with gzip.open(flags, "rt", encoding="utf-8") as stream:
                outcomes = [json.loads(line) for line in stream]
            self.assertEqual(report["audited_questions"], 2)
            self.assertEqual(report["distinct_visible_content_groups_with_conflicting_keys"], 1)
            self.assertTrue(all("same_visible_content_conflicting_answer_keys" in q["flags"]
                                for q in outcomes))
            self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), report)


if __name__ == "__main__":
    unittest.main()
