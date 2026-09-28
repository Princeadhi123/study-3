"""Tests explicit private 40-MCQ selection and independent answer-key checks."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from build_text_only_bank import build
from test_mcq_test import make_bank


class CuratedBankTests(unittest.TestCase):
    def build(self, alter=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory = root / "inventory.gz"
            flags = root / "flags.gz"
            selection_path = root / "selection.json"
            bank = make_bank()
            for q in bank["questions"]:
                q["skill_name"] = bank["skill_names"][q["skill_id"]]
                q["in_model_catalog"] = True
                q["review_status"] = "unreviewed"
            selection = {"skills": {skill: [
                [q["question_id"], q["options"][q["answer_index"]]]
                for q in bank["questions"] if q["skill_id"] == skill]
                for skill in bank["skill_names"]}}
            if alter:
                alter(bank, selection)
            with gzip.open(inventory, "wt", encoding="utf-8") as out, gzip.open(
                    flags, "wt", encoding="utf-8") as flag_out:
                for q in bank["questions"]:
                    out.write(json.dumps(q) + "\n")
                    flag_out.write(json.dumps({"question_id": q["question_id"],
                                               "skill_id": q["skill_id"],
                                               "flags": []}) + "\n")
            selection_path.write_text(json.dumps(selection), encoding="utf-8")
            return build(inventory, flags, selection_path)

    def test_repeated_templates_with_distinct_prompts(self):
        def repeat(bank, _):
            for q in bank["questions"][20:]:
                q["exercise_id"] = bank["questions"][0]["exercise_id"]
        bank, report = self.build(repeat)
        self.assertEqual(report["question_count"], 40)
        self.assertEqual(report["distinct_visible_prompts"], 40)
        self.assertGreater(report["repeated_template_count"], 0)
        self.assertEqual(bank["review_status"], "text_only_checked; not educator_approved")
        self.assertEqual(len(bank["questions"]), 40)

    def test_rejects_duplicate_prompt(self):
        def duplicate(bank, _):
            bank["questions"][20]["text"] = bank["questions"][0]["text"].upper()
        with self.assertRaisesRegex(ValueError, "distinct visible prompts"):
            self.build(duplicate)

    def test_rejects_incorrect_manual_answer(self):
        def mismatch(_, selection):
            selection["skills"]["sA"][0][1] = "not an offered answer"
        with self.assertRaisesRegex(ValueError, "Answer key disagrees"):
            self.build(mismatch)

    def test_rejects_missing_visual(self):
        def visual(bank, _):
            bank["questions"][0]["text"] = "Mikä on kuvan kulma?"
        with self.assertRaisesRegex(ValueError, "Visual"):
            self.build(visual)


if __name__ == "__main__":
    unittest.main()
