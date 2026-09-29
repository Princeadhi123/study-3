import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

import paths
from conformal_calibrate import conformal_quantile
from conformal_gate import ConformalGate


class ConformalQuantileTests(unittest.TestCase):
    def test_interior_rank_is_exact_order_statistic(self):
        scores = np.array([9, 1, 7, 3, 5, 0, 8, 2, 6, 4], dtype=float)
        self.assertEqual(conformal_quantile(scores, 0.20), 8.0)

    def test_repeated_scores_use_exact_rank(self):
        scores = np.array([0.9, 0.1, 0.4, 0.1, 0.2])
        self.assertEqual(conformal_quantile(scores, 0.40), 0.4)

    def test_last_available_rank(self):
        self.assertEqual(conformal_quantile(np.arange(9), 0.10), 8.0)

    def test_insufficient_and_empty_calibration_abstain(self):
        self.assertEqual(conformal_quantile(np.arange(8), 0.10), float("inf"))
        self.assertEqual(conformal_quantile(np.array([]), 0.10), float("inf"))


class GateArtifactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.k10 = root / "k10_calibration.json"
        self.k5 = root / "k5_calibration.json"
        self.coverage = root / "k10_coverage.json"
        self.k5_coverage = root / "k5_coverage.json"
        for path, k in ((self.k10, 10), (self.k5, 5)):
            path.write_text(json.dumps({
                "alpha": 0.1,
                "item_level": {"groups": {"warm": {"q_hat": {"0": 0.9, "1": 0.3}}}},
                "checkpoint_level": {"k": k, "sigma_floor": 0.02},
            }), encoding="utf-8")
        self.coverage.write_text(json.dumps({
            "alpha": 0.1,
            "checkpoint_level": [{"label": "eval_warm", "empirical_coverage": 0.9}],
        }), encoding="utf-8")
        self.k5_coverage.write_text(json.dumps({
            "alpha": 0.1,
            "checkpoint_level": [{"label": "eval_warm", "empirical_coverage": 0.89}],
        }), encoding="utf-8")
        self.missing_catalog = root / "missing_catalog.csv"

    def tearDown(self):
        self.tmp.cleanup()

    def test_default_loads_active_corrected_pair(self):
        with patch.object(paths, "ACTIVE_CALIBRATION", self.k10), \
                patch.object(paths, "ACTIVE_COVERAGE_REPORT", self.coverage):
            gate = ConformalGate.load(skill_catalog_path=self.missing_catalog)
        self.assertEqual(gate.k, 10)
        self.assertEqual(gate._empirical_coverage("warm"), 0.9)

    def test_custom_calibration_does_not_reuse_active_coverage(self):
        with patch.object(paths, "ACTIVE_COVERAGE_REPORT", self.coverage):
            gate = ConformalGate.load(
                calibration_path=self.k5, skill_catalog_path=self.missing_catalog)
        self.assertEqual(gate.k, 5)
        self.assertIsNone(gate._empirical_coverage("warm"))
        paired = ConformalGate.load(
            calibration_path=self.k5, coverage_path=self.k5_coverage,
            skill_catalog_path=self.missing_catalog)
        self.assertEqual(paired._empirical_coverage("warm"), 0.89)

    def test_missing_default_coverage_fails_closed(self):
        with patch.object(paths, "ACTIVE_CALIBRATION", self.k10), \
                patch.object(paths, "ACTIVE_COVERAGE_REPORT", Path(self.tmp.name) / "missing.json"):
            with self.assertRaises(FileNotFoundError):
                ConformalGate.load(skill_catalog_path=self.missing_catalog)

    def test_mismatched_alpha_is_rejected(self):
        bad = Path(self.tmp.name) / "wrong_alpha.json"
        bad.write_text(json.dumps({"alpha": 0.2}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "alpha disagree"):
            ConformalGate.load(
                calibration_path=self.k5, coverage_path=bad,
                skill_catalog_path=self.missing_catalog)


if __name__ == "__main__":
    unittest.main()
