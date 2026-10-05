"""Independent mathematics, compatibility, and research-only bank assembly."""
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import numpy as np

from build_evaluation_banks import (
    assemble, attach_compatibility, embedding_matches, expand_decisions,
    independently_check, linear,
)


def question(text, options, index):
    return {"text": text, "options": options, "answer_index": index}


class IndependentMathTests(unittest.TestCase):
    def test_percent_divisibility_prime_and_largest_fraction(self):
        cases = [
            ("percent_of", question("25 % luvusta 200", ["25", "50", "100"], 1)),
            ("divisibility", question("28 on jaollinen luvulla...", ["3", "7", "9"], 1)),
            ("prime_identification", question("Alkuluku?", ["4", "6", "5"], 2)),
            ("largest_fraction", question("Suurin murtoluku?", ["3/8", "5/8", "2/8"], 1)),
            ("half_matching", question("puolikas", ["1/6", "2/2", "5/10"], 2)),
        ]
        for family, q in cases:
            with self.subTest(family=family):
                self.assertEqual(independently_check(q, family)[0], q["answer_index"])

    def test_truth_of_mixed_fractions_and_nonsemantic_distractor_note(self):
        for text, index in (("3 1/8 = 19/8", 1), ("3 4/5 = 19/5", 0),
                            ("14 1/4 = 56/4", 1), ("1/2 = 8/16", 0)):
            q = question(text, ["tosi", "valhe", "maybe", "heart"], index)
            checked, _, notes = independently_check(q, "fraction_equality")
            self.assertEqual(checked, index)
            self.assertTrue(notes)

    def test_simplification_rejects_equivalent_uncollected_distractor(self):
        q = question("Sievenn\u00e4  2x \u2212 4x",
                     ["2x \u2212 4x", "\u22122x", "6x"], 1)
        self.assertEqual(independently_check(q, "linear_simplification")[0], 1)
        self.assertEqual(linear("x + y"), {"x": 1, "y": 1})
        self.assertEqual(linear("-3x + x"), {"x": -2})
        with self.assertRaises(ValueError):
            linear("x + x", simplified=True)
        with self.assertRaises(ValueError):
            linear("x*y")

    def test_contradictory_source_key_or_two_valid_choices_fail(self):
        q = question("50 % luvusta 100", ["50", "100"], 1)
        with self.assertRaisesRegex(ValueError, "contradicts source"):
            independently_check(q, "percent_of")
        q = question("puolikas", ["1/2", "2/4"], 0)
        with self.assertRaisesRegex(ValueError, "exactly one"):
            independently_check(q, "half_matching")
        q = question("50 % luvusta 100", ["50", "100"], True)
        with self.assertRaisesRegex(ValueError, "Malformed"):
            independently_check(q, "percent_of")

    def test_review_ranges_must_cover_every_candidate_once(self):
        queue = {"candidates": [{"review_number": 1}, {"review_number": 2}]}
        decisions = {"accepted_families": {"percent_of": [[1, 1]]},
                     "excluded_ranges": [{"range": [2, 2], "reason": "missing context"}]}
        self.assertEqual(expand_decisions(queue, decisions)[2][0], "excluded")
        decisions["excluded_ranges"] = []
        with self.assertRaisesRegex(ValueError, "every candidate"):
            expand_decisions(queue, decisions)

    def test_simplification_accepts_lauseke_prefix_variants(self):
        for text in ("Sievenn\u00e4 x + x", "Sievenn\u00e4 lauseke: x + x",
                     "Sievenn\u00e4 lauseke:x + x", "Sievenn\u00e4\t lauseke:\n x + x"):
            with self.subTest(text=text):
                q = question(text, ["2x", "x"], 0)
                self.assertEqual(independently_check(q, "linear_simplification")[0], 0)

    def test_simplification_rejects_missing_or_wrong_prefix(self):
        for text in ("Sievenn\u00e4", "Sievenn\u00e4 lauseke:",
                     "Sievenn\u00e4x + x", "Other x + x"):
            with self.subTest(text=text):
                q = question(text, ["2x", "x"], 0)
                with self.assertRaises(ValueError):
                    independently_check(q, "linear_simplification")

    def test_unknown_family_rejected_by_registry(self):
        q = question("25 % luvusta 200", ["25", "50"], 1)
        with self.assertRaisesRegex(ValueError, "Unknown independently reviewed family"):
            independently_check(q, "missing")

    def test_overlapping_review_decisions_fail(self):
        queue = {"candidates": [{"review_number": 1}, {"review_number": 2}]}
        for decisions in (
                {"accepted_families": {"percent_of": [[1, 2]], "half_matching": [[2, 2]]},
                 "excluded_ranges": []},
                {"accepted_families": {"percent_of": [[1, 2]]},
                 "excluded_ranges": [{"range": [2, 2], "reason": "excluded"}]}):
            with self.subTest(decisions=decisions):
                with self.assertRaisesRegex(ValueError, "Overlapping review decisions"):
                    expand_decisions(queue, decisions)

    def test_duplicate_review_numbers_fail(self):
        queue = {"candidates": [{"review_number": 1}, {"review_number": 1}]}
        decisions = {"accepted_families": {"percent_of": [[1, 1]]},
                     "excluded_ranges": []}
        with self.assertRaisesRegex(ValueError, "every candidate"):
            expand_decisions(queue, decisions)


def checked_rows():
    skills = {f"s{i}": f"Skill {i}" for i in range(4)}
    rows = []
    for regime in ("warm", "cold"):
        for skill in skills:
            for index in range(10):
                rows.append({
                    "regime": regime, "review_number": len(rows) + 1,
                    "review_status": "accepted", "kt_compatibility": {"compatible": True},
                    "question": {
                        "question_id": f"{regime}-{skill}-{index}", "skill_id": skill,
                        "item_id": f"{regime}-{skill}-{index}", "exercise_id": f"ex-{skill}",
                        "text": f"Prompt {skill} {index}", "options": ["correct", "wrong"],
                        "answer_index": 0, "content_text": f"content-{regime}-{skill}-{index}"},
                })
    return rows, skills


class AssemblyTests(unittest.TestCase):
    def test_balanced_banks_keep_sources_and_are_not_approved_serving_banks(self):
        rows, skills = checked_rows()
        banks, selected, shortages = assemble(rows, skills)
        self.assertFalse(shortages)
        for regime, bank in banks.items():
            self.assertEqual(len(bank["questions"]), 40)
            self.assertEqual(Counter(q["skill_id"] for q in bank["questions"][:20]),
                             {s: 5 for s in skills})
            self.assertNotEqual(bank["protocol"], "offline_mcq_demo_only")
            self.assertIn("educator_approval_pending", bank["review_status"])
            self.assertEqual(bank["questions"][0], selected[regime][0]["question"])

    def test_missing_compatible_question_reports_shortfall_without_padding(self):
        rows, skills = checked_rows()
        rows[0]["kt_compatibility"]["compatible"] = False
        banks, _, shortages = assemble(rows, skills)
        self.assertNotIn("warm", banks)
        self.assertIn("cold", banks)
        self.assertEqual(shortages[0]["available"], 9)

    def test_every_option_embedding_required_and_cold_id_mapping_checked(self):
        rows, skills = checked_rows()
        row = rows[40]
        q = row["question"]
        vocab = {"skill_vocab": {s: i + 2 for i, s in enumerate(skills)}, "item_vocab": {}}
        attach_compatibility([row], vocab, {q["content_text"]: 3, "correct": 4})
        self.assertEqual(row["kt_compatibility"]["item_index"], 1)
        self.assertFalse(row["kt_compatibility"]["compatible"])
        self.assertEqual(row["kt_compatibility"]["missing_embedding_texts"], ["wrong"])
        row["regime"] = "warm"
        with self.assertRaisesRegex(ValueError, "regime disagrees"):
            attach_compatibility([row], vocab, {})

    def test_embedding_membership_uses_exact_text_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "embeddings.npz"
            np.savez(path, texts=np.array(["pad", "question", "option"], dtype=object),
                     vectors=np.zeros((3, 2)))
            self.assertEqual(embedding_matches(path, {"question", "option", "missing"}),
                             {"question": 1, "option": 2})


if __name__ == "__main__":
    unittest.main()
