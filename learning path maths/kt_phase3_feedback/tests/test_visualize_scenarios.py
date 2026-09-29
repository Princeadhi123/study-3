"""Renderer checks for visualize_scenarios (matplotlib optional)."""
import tempfile
import unittest
from pathlib import Path

from scenario_report import compile_report
from scenario_runner import run_scenario
from session_store import SessionStore
from tests.helpers import make_bank
from visualize_scenarios import _matrix_data, render, render_matrix

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
            self.assertEqual(names, set(p.name for p in out.iterdir()))
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


class VisualizeMatrixTests(unittest.TestCase):
    def matrix(self):
        results = []
        kinds = ("within_half_order", "repeated_items_prior_correct",
                 "repeated_items_prior_incorrect", "unknown_item_ids_same_text")
        for profile in ("learning", "fatigue"):
            for seed in (11, 23):
                results.append({
                    "scenario": {"profile": profile, "seed": seed},
                    "base_bank_sha256": "bank",
                    "baseline": {"bank_sha256": "bank", "responses": [{}] * 40,
                                 "conformal": {"checkpoints": {
                                     "midpoint": dict.fromkeys(range(4)),
                                     "end": dict.fromkeys(range(4))}}},
                    "variants": [{"kind": kind, "comparison_to_baseline": {
                        "mean_absolute_kt_change": 0.1 + index / 10,
                        "item_status_changes": index + 1,
                        "checkpoint_status_changes": index}}
                        for index, kind in enumerate(kinds)],
                })
        return {"schema": "phase3_research_matrix_batch_v1",
                "scope": "researcher_only", "base_bank_sha256": "bank",
                "run_count": len(results), "results": results}

    def test_research_matrix_values_and_rejections(self):
        batch = self.matrix()
        data = _matrix_data(batch)
        self.assertEqual((0.1, 1 / 40, 0),
                         data["learning"][11]["within_half_order"])
        self.assertEqual((0.4, 4 / 40, 3 / 8),
                         data["fatigue"][23]["unknown_item_ids_same_text"])
        with self.assertRaises(ValueError):
            _matrix_data({**batch, "scope": "student"})
        with self.assertRaises(ValueError):
            _matrix_data({**batch, "run_count": 1})
        with self.assertRaises(ValueError):
            _matrix_data({**batch, "results": batch["results"][:-1],
                          "run_count": 3})

    @unittest.skipUnless(HAS_MATPLOTLIB, "matplotlib not installed")
    def test_research_matrix_figures_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "matrix_figures"
            batch = self.matrix()
            files = render_matrix(batch, out)
            self.assertEqual(4, len(files))
            self.assertEqual({".png", ".svg"}, {p.suffix for p in files})
            self.assertTrue(all(p.stat().st_size > 0 for p in files))
            with self.assertRaises(FileExistsError):
                render_matrix(batch, out)


if __name__ == "__main__":
    unittest.main()
