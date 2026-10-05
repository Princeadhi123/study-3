"""Exact solver versus exhaustive fixtures, plus closed-review edge cases."""
import itertools
import random
import unittest

from optimize_global_banks import optimal_assignment, optimize
from review_global_bank_candidates import DRAGON_QUARTER, closed_review


def row(skill, prompt, students, regime="warm", qid=None):
    return {"question": {"skill_id": skill, "text": prompt,
                         "question_id": qid or skill + prompt},
            "regime": regime, "support": {"students": students,
                                          "with_history_students": students,
                                          "n": students}}


class ExactAssignmentTests(unittest.TestCase):
    def test_shared_prompt_requires_reassignment_not_greedy_selection(self):
        rows = [row("a", "shared", 10), row("a", "other", 8),
                row("b", "shared", 9), row("b", "third", 1)]
        selected = optimal_assignment(rows, ["a", "b"], 1)
        self.assertEqual(sum(r["support"]["students"] for r in selected), 17)

    def test_infeasibility_and_option_variant_support(self):
        self.assertIsNone(optimal_assignment(
            [row("a", "shared", 9), row("b", "shared", 8)], ["a", "b"], 1))
        selected = optimal_assignment(
            [row("a", "same", 3, qid="low"), row("a", "same", 7, qid="high")], ["a"], 1)
        self.assertEqual(selected[0]["question"]["question_id"], "high")

    def test_matches_independent_brute_force_for_small_assignment_graphs(self):
        rng = random.Random(20261005)
        skills = ["a", "b", "c"]
        for _ in range(25):
            rows = [row(s, str(p), rng.randint(2, 20))
                    for s in skills for p in range(4)]
            brute = max(sum(r["support"]["students"] for r in choice)
                        for choice in itertools.product(
                            *[[r for r in rows if r["question"]["skill_id"] == s] for s in skills])
                        if len({r["question"]["text"] for r in choice}) == len(skills))
            selected = optimal_assignment(rows, skills, 1)
            self.assertEqual(sum(r["support"]["students"] for r in selected), brute)

    def test_primary_weakest_support_objective_precedes_large_total(self):
        pool = []
        for index in range(5):
            for regime in ("warm", "cold"):
                students = (100 if regime == "warm" else 2) if index == 0 else 3
                pool.extend(row(str(index), f"topic{index}-prompt{p}", students, regime)
                            for p in range(10))
        winner, certificate = optimize(pool)
        self.assertEqual(certificate["objective"]["minimum_students_per_rendering"], 3)
        self.assertEqual(certificate["objective"]["sum_per_rendering_student_counts"], 240)
        self.assertNotIn("0", {r["question"]["skill_id"] for r in winner["warm"]})

    def test_missing_regime_is_pruned_without_mutating_iteration(self):
        with self.assertRaisesRegex(ValueError, "No eligible"):
            optimize([row("a", str(p), 5) for p in range(12)])


class ClosedContentTests(unittest.TestCase):
    def test_explicit_fraction_quantity_and_word_problem(self):
        q = {"skill_id": "skill_e8d49a51337a", "text": "Ota 2\u20443 luvusta 12",
             "options": ["24", "4", "8", "3"], "answer_index": 2}
        self.assertEqual(closed_review(q)[0]["family"], "fraction_of_quantity")
        q.update(text=DRAGON_QUARTER, options=["4", "12", "6", "9", "11", "7"],
                 answer_index=5)
        self.assertEqual(closed_review(q)[0]["family"], "fraction_word_problem")

    def test_ambiguous_geometry_and_mixed_number_not_reconstructed(self):
        q = {"skill_id": "skill_5f921ad8dd86",
             "text": "\u03b1 = 15\u00b0, \u03b2 = 160\u00b0, \u03b3 = ?",
             "options": ["20\u00b0", "15\u00b0", "10\u00b0", "5\u00b0"], "answer_index": 3}
        self.assertIsNone(closed_review(q)[0])
        q.update(skill_id="skill_d65977bf1b26", text="Ota 2\u20443 luvusta 4",
                 options=["21\u20443", "22\u20443", "12\u20443", "3"], answer_index=1)
        self.assertIn("Ambiguous", closed_review(q)[1])

    def test_angle_definition_key_and_unknown_stem(self):
        q = {"skill_id": "skill_5f921ad8dd86", "text": "suora kulma",
             "options": ["0\u00b0", "180\u00b0", "360\u00b0", "90\u00b0"], "answer_index": 3}
        self.assertEqual(closed_review(q)[0]["family"], "angle_type")
        q["answer_index"] = 1
        with self.assertRaises(ValueError):
            closed_review(q)
        q["text"] = "A previously unseen complete geometry question"
        with self.assertRaises(LookupError):
            closed_review(q)

    def test_blue_circle_reference_is_not_a_standalone_geometry_question(self):
        q = {"skill_id": "skill_5f921ad8dd86",
             "text": "Miss\u00e4 sininen ympyr\u00e4 sivuaa kolmiota?",
             "options": ["Kolmion sivujen keskipisteiss\u00e4",
                         "Sininen ympyr\u00e4 ei sivua kolmiota",
                         "Kolmion jokaisessa kulmassa"],
             "answer_index": 2}
        self.assertIn("Missing", closed_review(q)[1])


if __name__ == "__main__":
    unittest.main()
