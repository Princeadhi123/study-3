"""Tests controlled approval of the checked text-only bank."""
import gzip
import json
import tempfile
import unittest
from pathlib import Path

from approve_text_only_bank import approved_copy
from build_text_only_bank import build
from test_mcq_test import make_bank


class ApproveTextOnlyBankTests(unittest.TestCase):
    def approve(self, alter_source=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inventory = root / "inventory.gz"
            flags = root / "flags.gz"
            selection_path = root / "selection.json"
            source_path = root / "source.json"
            bank = make_bank()
            for q in bank["questions"]:
                q["skill_name"] = bank["skill_names"][q["skill_id"]]
                q["in_model_catalog"] = True
                q["review_status"] = "unreviewed"
            selection = {"skills": {skill: [
                [q["question_id"], q["options"][q["answer_index"]]]
                for q in bank["questions"] if q["skill_id"] == skill]
                for skill in bank["skill_names"]}}
            with gzip.open(inventory, "wt", encoding="utf-8") as out, gzip.open(
                    flags, "wt", encoding="utf-8") as flag_out:
                for q in bank["questions"]:
                    out.write(json.dumps(q) + "\n")
                    flag_out.write(json.dumps({"question_id": q["question_id"],
                                               "skill_id": q["skill_id"],
                                               "flags": []}) + "\n")
            selection_path.write_text(json.dumps(selection), encoding="utf-8")
            source, _ = build(inventory, flags, selection_path)
            if alter_source:
                alter_source(source)
            source_path.write_text(json.dumps(source), encoding="utf-8")
            return approved_copy(inventory, flags, selection_path,
                                 source_path, "test reviewer"), source

    def test_approved_copy_preserves_checked_questions(self):
        approved, source = self.approve()
        self.assertEqual(approved["review_status"], "approved")
        self.assertEqual(approved["questions"], source["questions"])
        self.assertEqual(approved["approval"]["scope"],
                         "offline MCQ prototype only")
        self.assertEqual(source["review_status"],
                         "text_only_checked; not educator_approved")

    def test_rejects_source_that_differs_from_checked_selection(self):
        def alter(source):
            source["questions"][0]["text"] = "changed after review"
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.approve(alter)

    def test_rejects_an_already_approved_source(self):
        def alter(source):
            source["review_status"] = "approved"
        with self.assertRaisesRegex(ValueError, "before approval"):
            self.approve(alter)


if __name__ == "__main__":
    unittest.main()
