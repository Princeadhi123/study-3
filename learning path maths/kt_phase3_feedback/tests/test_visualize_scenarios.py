"""Renderer checks for visualize_scenarios (matplotlib optional)."""
import tempfile
import unittest
from pathlib import Path

from scenario_report import compile_report
from scenario_runner import run_scenario
from session_store import SessionStore
from tests.helpers import make_bank
from visualize_scenarios import render

try:
    import matplotlib  # noqa: F401
    HAS_MATPLOTLIB = True
except ImportError:
    HAS_MATPLOTLIB = False


def _report(bank, tmp, seeds=(42,)):
    store = SessionStore(Path(tmp) / f"sessions_{len(list(Path(tmp).iterdir()))}")
    results = [run_scenario(bank, {"profile": "learning", "seed": seed}, store)
               for seed in seeds]
    return compile_report(results)


class VisualizeScenariosTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_bank()

    def test_rejects_wrong_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                render({"schema": "not_the_report"}, Path(tmp))

    def test_sanitized_filename_collision_and_no_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "figures"
            report = _report(self.bank, tmp, seeds=(42,))
            # "a b" and "a_b" sanitize to the same filename for the same
            # seed/skill; collision is detected before matplotlib is needed.
            clashing = dict(report)
            clashing["trace"] = (
                [{**row, "scenario": "a b"} for row in report["trace"]]
                + [{**row, "scenario": "a_b"} for row in report["trace"]])
            with self.assertRaises(ValueError):
                render(clashing, out)
        if not HAS_MATPLOTLIB:
            self.skipTest("matplotlib not installed")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "figures"
            report = _report(self.bank, tmp)
            render(report, out)
            with self.assertRaises(FileExistsError):
                render(report, out)

    @unittest.skipUnless(HAS_MATPLOTLIB, "matplotlib not installed")
    def test_renders_pngs_and_seed_aggregation(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "figures"
            report = _report(self.bank, tmp, seeds=(42, 43))
            written = render(report, out, aggregate=True)
            self.assertTrue(written)
            names = {p.name for p in written}
            self.assertEqual(set(p.name for p in out.iterdir()), names)
            # 4 skills x 2 seeds trace figures + 4 aggregate figures.
            self.assertEqual(12, len(written))
            self.assertEqual(4, len([n for n in names
                                     if n.startswith("aggregate_learning_")]))
            for path in written:
                self.assertTrue(path.exists())
                self.assertGreater(path.stat().st_size, 0)
                self.assertEqual(".png", path.suffix)

    @unittest.skipUnless(HAS_MATPLOTLIB, "matplotlib not installed")
    def test_long_conformal_annotation_renders(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "figures"
            report = _report(self.bank, tmp, seeds=(42,))
            for row in report["summary"]:
                row["conformal_status"] = "UNCERTAIN_BEHAVIOR"
                row["conformal_lower"] = 0.1234
                row["conformal_upper"] = 0.9876
                row["conformal_calibration_status"] = (
                    "approximate_k5_not_calibrated_k10"
                    if row["checkpoint"] == "midpoint" else
                    "k10_protocol_not_validated_for_synthetic_fixed_bank")
            written = render(report, out)
            self.assertEqual(4, len(written))
            for path in written:
                self.assertTrue(path.exists())
                self.assertGreater(path.stat().st_size, 0)

    def test_caption_lines_are_humanized_and_bounded(self):
        from visualize_scenarios import _caption_lines, _conformal_for
        summary = [{"scenario": "s", "seed": 1, "skill_id": "sA",
                    "checkpoint": "midpoint",
                    "conformal_status": "UNCERTAIN_BEHAVIOR",
                    "conformal_lower": 0.1, "conformal_upper": 0.9,
                    "conformal_calibration_status":
                        "approximate_k5_not_calibrated_k10"},
                   {"scenario": "s", "seed": 1, "skill_id": "sA",
                    "checkpoint": "end",
                    "conformal_status": "INFORMATIVE",
                    "conformal_lower": 0.2, "conformal_upper": 0.8,
                    "conformal_calibration_status":
                        "k10_protocol_not_validated_for_synthetic_fixed_bank"}]
        notes = _conformal_for(summary, "s", 1, "sA")
        self.assertEqual(2, len(notes))
        self.assertIn("k=5 approx", notes[0])
        self.assertIn("k=10 exploratory", notes[1])
        self.assertIn("fixed-bank coverage unverified", notes[1])
        self.assertNotIn("_", " ".join(notes))
        joined = "\n".join(_caption_lines(notes))
        self.assertIn("distinct estimands", joined)
        # both checkpoint cautions survive intact — no truncation
        self.assertIn("midpoint (k=5 approx, uncalibrated): "
                      "UNCERTAIN BEHAVIOR [0.100, 0.900]", joined)
        self.assertIn("end (k=10 exploratory, fixed-bank coverage "
                      "unverified): INFORMATIVE [0.200, 0.800]", joined)


if __name__ == "__main__":
    unittest.main()
