"""Oracle and source-masking contracts; no human judgments or model calls."""
import copy
import json
import unittest

from compare_practice import (
    assumed_probability, blind_pack, difficulty_assignment, oracle_study, response_draw)


def fixtures():
    questions = {
        "q1": {"question_id": "q1", "skill_id": "skill", "text": "One plus one?",
               "options": ["1", "2", "3"]},
        "q2": {"question_id": "q2", "skill_id": "skill", "text": "Two plus one?",
               "options": ["2", "3", "4"]},
    }
    case = {"case_id": "case1", "skill_names": {"skill": "Arithmetic"},
            "skill_counts": {"skill": {"correct": 5, "incorrect": 5, "out_of": 10}},
            "review_summary": [{"topic": "Arithmetic", "correct": 5, "out_of": 10}],
            "review_answers": [], "baseline_question_id": "q1", "selected_question_id": "q2"}
    cases = [dict(copy.deepcopy(case), case_id=f"case{i}") for i in range(10)]
    return cases, questions


class SourceMaskTests(unittest.TestCase):
    def test_method_labels_and_ids_hidden_with_balanced_side_assignment(self):
        cases, questions = fixtures()
        pack, key = blind_pack(cases, questions)
        wire = json.dumps(pack)
        for forbidden in ('"baseline"', '"kt"', '"case_id"', '"question_id"', '"p_correct"'):
            self.assertNotIn(forbidden, wire)
        self.assertEqual(len(pack["tasks"]), 10)
        self.assertEqual(len({t["task_id"] for t in pack["tasks"]}), 10)
        self.assertEqual(sum(k["A"] == "baseline" for k in key), 5)
        self.assertEqual(blind_pack(cases, questions), (pack, key))

    def test_missing_recommendations_remain_visible_not_dropped(self):
        cases, questions = fixtures()
        cases[0]["selected_question_id"] = None
        cases[1]["selected_question_id"] = cases[1]["baseline_question_id"] = None
        pack, key = blind_pack(cases, questions)
        tasks = {k["case_id"]: next(t for t in pack["tasks"] if t["task_id"] == k["task_id"])
                 for k in key}
        self.assertEqual(sum(a["suggested_question"] is None for a in tasks["case0"]["alternatives"]), 1)
        self.assertEqual(sum(a["suggested_question"] is None for a in tasks["case1"]["alternatives"]), 2)


class OracleTests(unittest.TestCase):
    def test_probability_assumptions_and_monotonicity(self):
        # At c=n/2 and difficulty=0, sigmoid=0.5.
        self.assertAlmostEqual(assumed_probability(5, 10, 4, 0), 0.25 + 0.70 * 0.5)
        self.assertGreater(assumed_probability(8, 10, 4, 0),
                           assumed_probability(2, 10, 4, 0))
        self.assertGreater(assumed_probability(5, 10, 4, -1.5),
                           assumed_probability(5, 10, 4, 1.5))
        for c in range(11):
            self.assertTrue(0 < assumed_probability(c, 10, 3, 3) < 1)

    def test_item_assignments_independent_of_input_order(self):
        _, questions = fixtures()
        before = difficulty_assignment(list(questions.values()), seed=17)
        after = difficulty_assignment(list(questions.values())[::-1], seed=17)
        self.assertEqual(before, after)
        self.assertTrue(all(-3 <= b <= 3 for b in before.values()))

    def test_no_KT_probabilities_or_human_ratings_used(self):
        cases, questions = fixtures()
        before = oracle_study(cases, questions)
        for case in cases:
            case["p_correct"] = float("nan")
            case["reviewer_rating"] = "fabricated"
        self.assertEqual(oracle_study(cases, questions), before)
        self.assertEqual(before["human_reviews_completed"], 0)
        self.assertFalse(before["KT_probabilities_used_to_generate_oracle"])

    def test_paired_denominator_excludes_abstention_but_reports_availability(self):
        cases, questions = fixtures()
        cases[0]["selected_question_id"] = None
        cases[1]["selected_question_id"] = cases[1]["baseline_question_id"] = None
        result = oracle_study(cases, questions)
        self.assertEqual(result["availability"], {"baseline_only": 1, "neither_selected": 1,
                                                 "both_selected": 8})
        self.assertTrue(all(w["paired_cases"] == 8 for w in result["worlds"]))

    def test_same_question_has_same_counterfactual_response_for_both_policies(self):
        cases, questions = fixtures()
        for case in cases:
            case["selected_question_id"] = case["baseline_question_id"]
        result = oracle_study(cases, questions)
        for world in result["worlds"]:
            self.assertEqual(world["kt_minus_baseline_distance"], 0)
            for case in world["outcomes"]:
                self.assertEqual(case["choices"]["baseline"], case["choices"]["kt"])
        self.assertEqual(response_draw("case", "q1"), response_draw("case", "q1"))


if __name__ == "__main__":
    unittest.main()
