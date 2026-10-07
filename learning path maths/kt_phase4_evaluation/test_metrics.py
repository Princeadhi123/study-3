"""Analytic fixtures for metric definitions, ties and student dependence."""
import unittest

import numpy as np

from metrics import calibration_bins, evaluate, interval, scores, weighted_auc


class MetricTests(unittest.TestCase):
    def test_auc_ties_and_weighted_replication(self):
        y = np.array([0, 1, 0, 1], dtype=float)
        p = np.array([0.2, 0.2, 0.8, 0.9])
        weights = np.array([2, 1, 1, 3])
        replicated_y = np.repeat(y, weights)
        replicated_p = np.repeat(p, weights)
        expected = np.mean([float(a > b) + 0.5 * float(a == b)
                            for a in replicated_p[replicated_y == 1]
                            for b in replicated_p[replicated_y == 0]])
        self.assertEqual(weighted_auc(y, p, weights), expected)
        self.assertIsNone(weighted_auc(np.ones(4), p, weights))

    def test_student_balancing_is_not_event_weighting(self):
        y = np.array([1, 1, 0], dtype=float)
        p = np.array([0.5, 0.5, 1.0])
        result = scores(y, p, np.ones(3), np.array([0, 0, 1]))
        self.assertEqual(result["brier"], 0.5)
        self.assertEqual(result["student_balanced_brier"], 0.625)

    def test_calibration_boundaries_and_empty_bins(self):
        result = calibration_bins(np.array([0, 1]), np.array([0.0, 1.0]))
        self.assertEqual(result["ece"], 0)
        self.assertEqual(result["bins"][0]["n"], 1)
        self.assertEqual(result["bins"][9]["n"], 1)
        self.assertIsNone(result["bins"][1]["observed_rate"])

    def test_single_class_auc_and_identical_paired_predictions(self):
        rows = [{"student_code": i, "question_id": str(i), "y": 1,
                 "a": 0.8, "b": 0.8} for i in range(3)]
        result = evaluate(rows, ["a", "b"], [("a", "b")], draws=100)
        self.assertIsNone(result["models"]["a"]["auc"])
        self.assertEqual(result["models"]["a"]["auc_reason"], "single_observed_class")
        self.assertEqual(result["paired_differences"]["a_minus_b"]["brier"]["estimate"], 0)
        self.assertEqual(result["paired_differences"]["a_minus_b"]["brier"]["interval"]["lower"], 0)

    def test_interval_suppression_and_probability_validation(self):
        self.assertIsNone(interval([0.2] * 2000, 1)["lower"])
        self.assertIsNone(interval([0.2] * 99, 4)["lower"])
        with self.assertRaises(ValueError):
            evaluate([{"student_code": 0, "question_id": "q", "y": 1, "p": 2}],
                     ["p"], draws=100)

    def test_frozen_baseline_statistics_are_reused_without_replacement(self):
        rows = [{"student_code": i, "question_id": str(i), "y": i % 2,
                 "baseline": 0.6, "kt": 0.7} for i in range(4)]
        original = evaluate(rows, ["baseline"], draws=100)
        frozen = original["models"]
        result = evaluate(rows, ["baseline", "kt"], [("kt", "baseline")],
                          draws=100, frozen=frozen)
        self.assertEqual(result["models"]["baseline"], frozen["baseline"])


if __name__ == "__main__":
    unittest.main()
