"""Tests for the frozen provider-replay HTML renderer (pure fixtures)."""
import unittest

from render_provider_replay import REPORT_SCHEMA, render


def mini_report():
    return {
        "schema": REPORT_SCHEMA,
        "scope": "private_synthetic_review_not_learner_delivery",
        "source": {"file": "snapshot.json", "sha256": "abc123"},
        "reuse_policy": "identical_effective_requests_share_captured_"
                        "responses_including_failures",
        "latency_warning": "cached pipeline latency is replay "
                           "overhead, not hosted latency",
        "summary": {
            "scenario_labels": 1, "feedback_packages": 1,
            "end_packages": 0, "end_selection_matches_rules": 0,
            "fallback_packages": 0,
            "new_provider_calls_this_invocation": 0,
            "independent_provider_rerun_stability_tested": False,
            "educator_preference_labels_available": False,
            "educational_effectiveness_tested": False},
        "packages": [{
            "scenario": "demo<x>scenario",
            "group": "deterministic",
            "audience": "student", "checkpoint": "midpoint",
            "review": {
                "schema": "phase3_synthetic_feedback_review_v1",
                "status": "draft_not_for_learner_delivery",
                "audience": "student", "checkpoint": "midpoint",
                "sanitized_evidence": {},
                "selected_candidate_id": "neutral",
                "template_baseline": {
                    "candidate_id": "neutral",
                    "text": "Baseline <script>alert(1)</script> text."},
                "message": {
                    "candidate_id": "neutral",
                    "text": "Provider text <b>not markup</b>."},
                "requires_human_review": True,
                "trace": {"fallback_reason": None, "latency_ms": 1.5}},
            "selector_execution": {"status": "local_single_candidate",
                                   "reused": False},
            "generator_execution": {
                "capture_file": "aitta_abc.json",
                "reused": True,
                "origin": "hosted_combined_20261002_v2.json",
                "metadata": {"prompt_version": "synthetic_phrasing_v2",
                             "status": "completed",
                             "model_version": "openai/gpt-oss-120b",
                             "usage": {"prompt_tokens": 1,
                                       "completion_tokens": 1,
                                       "total_tokens": 2}},
                "original_call_latency_ms": None}}]}


class RenderTests(unittest.TestCase):
    def test_escapes_dynamic_content(self):
        page = render(mini_report())
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("demo&lt;x&gt;scenario", page)
        self.assertIn("&lt;b&gt;not markup&lt;/b&gt;", page)

    def test_review_labels_and_latency_warning(self):
        page = render(mini_report())
        self.assertIn("Frozen Scenario Review", page)
        self.assertIn("Synthetic research drafts - human review "
                      "required", page)
        self.assertIn("Replay wall time (includes cache overhead; not "
                      "hosted latency)", page)
        self.assertIn("cached pipeline latency is replay overhead, not "
                      "hosted latency", page)
        self.assertIn("not independent repeated calls", page)

    def test_fact_panels_render_raw_counts(self):
        page = render(mini_report())
        for label in ("Scenario labels", "Feedback packages",
                      "New provider calls (this invocation)",
                      "Packages with fallback"):
            self.assertIn(label, page)
        self.assertIn("Educator preference labels: no", page)

    def test_wrong_schema_rejected(self):
        report = mini_report()
        report["schema"] = "wrong_schema"
        with self.assertRaises(ValueError):
            render(report)
        with self.assertRaises(ValueError):
            render({"schema": REPORT_SCHEMA + "x"})
        with self.assertRaises(ValueError):
            render("not a dict")


if __name__ == "__main__":
    unittest.main()
