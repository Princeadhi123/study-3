"""Failed parity cannot silently become an ordinary passed evaluation."""
import tempfile
import unittest
from pathlib import Path

from metrics import conformal_item_report
from phase4_common import write_json
from summarize_replay import score_block_report, verify_inference_gate


class AnalysisGateTests(unittest.TestCase):
    def test_original_failure_stays_blocked_without_explicit_addendum_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "inference_report.json", {"status": "parity_failure_investigate"})
            with self.assertRaisesRegex(ValueError, "explicit qualified"):
                verify_inference_gate(root, False)

    def test_failed_cpu_verification_cannot_bypass_original_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_json(root / "inference_report.json", {"status": "parity_failure_investigate"})
            write_json(root / "cpu_reference_verification.json",
                       {"status": "failed", "violations": 1})
            with self.assertRaisesRegex(ValueError, "did not pass"):
                verify_inference_gate(root, True)

    def test_no_score_blocks_yields_limitation_not_coverage(self):
        result = score_block_report([], [], {})
        self.assertEqual(result["status"], "insufficient_genuine_selected_block_support")
        self.assertIsNone(result["coverage"])

    def test_item_sets_use_regime_label_thresholds_and_true_label_membership(self):
        calibration = {"alpha": 0.1, "item_level": {"groups": {
            "warm": {"q_hat": {"0": 0.7, "1": 0.2}}}}}
        rows = [{"student_code": 0, "regime": "warm", "y": 1, "p_history": 0.9},
                {"student_code": 1, "regime": "warm", "y": 0, "p_history": 0.2}]
        result = conformal_item_report(rows, calibration, draws=100)
        self.assertEqual(result["empirical_coverage"], 1)
        self.assertEqual(result["mean_set_size"], 1)
        self.assertEqual(result["singleton_fraction"], 1)


if __name__ == "__main__":
    unittest.main()
