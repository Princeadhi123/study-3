"""Checks complete MCQ extraction, deduplication, and conflicting-key exclusion."""
import tempfile
import unittest
from pathlib import Path

from mcq_inventory import build_inventory
from test_question_bank import make_row, write_catalog, write_source


class InventoryTests(unittest.TestCase):
    def build(self, rows):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "source.csv.gz"
            catalog = Path(tmp) / "catalog.csv"
            write_source(source, rows)
            write_catalog(catalog)
            return build_inventory(source, catalog)

    def test_keeps_every_rendering_and_skill_not_just_top_four(self):
        rows = [make_row("s1", 1), make_row("s1", 1),
                make_row("s1", 1, variant=2), make_row("s2", 2),
                make_row("s5", 3), make_row("s1", 4, exercise_family="open")]
        questions, report = self.build(rows)
        self.assertEqual(len(questions), 4)
        self.assertEqual({q["skill_id"] for q in questions}, {"s1", "s2", "s5"})
        self.assertFalse(next(q for q in questions if q["skill_id"] == "s5")["in_model_catalog"])
        self.assertTrue(all(q["review_status"] == "unreviewed" for q in questions))
        self.assertTrue(all(set(q) == {"question_id", "skill_id", "item_id",
                                        "exercise_id", "text", "options",
                                        "answer_index", "content_text", "skill_name",
                                        "in_model_catalog", "review_status"}
                            for q in questions))
        self.assertEqual(report["counts"]["mcq_events"], 5)
        self.assertEqual(report["counts"]["scoring_eligible_events"], 5)
        self.assertEqual(report["counts"]["distinct_scoring_eligible_questions"], 4)
        self.assertEqual(report["counts"]["questions_in_model_catalog"], 3)
        self.assertEqual(report["counts"]["source_events"], 6)

    def test_rejects_bad_key_and_ambiguous_answer_for_same_rendering(self):
        original = make_row("s1", 1)
        changed_key = make_row("s1", 1, options_json=(
            '[{"value":"distractor","correct":true},'
            '{"value":"the answer","correct":false}]'),
            correct_option_index="0", correct_option_value="distractor")
        rows = [original, changed_key, make_row("s2", 2),
                make_row("s3", 3, correct_option_value="incorrect")]
        questions, report = self.build(rows)
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["skill_id"], "s2")
        self.assertEqual(report["counts"]["not_scoring_eligible_events"], 1)
        self.assertEqual(report["counts"]["ambiguous_renderings"], 1)
        self.assertEqual(report["counts"]["excluded_ambiguous_questions"], 2)


if __name__ == "__main__":
    unittest.main()
