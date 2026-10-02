"""Tests for the observed-evidence feedback HTML renderer (fixtures)."""
import json
import tempfile
import unittest
from pathlib import Path

import render_evidence_feedback
from render_evidence_feedback import REPORT_SCHEMA, main, render


def _review(audience, checkpoint, text, selected="neutral",
            baseline="neutral"):
    return {
        "schema": "phase3_evidence_feedback_review_v1",
        "status": "draft_not_for_learner_delivery",
        "audience": audience, "checkpoint": checkpoint,
        "sanitized_evidence": {"total": {"correct": 1}},
        "candidates": [{
            "candidate_id": "review_x", "strategy": "focused_review",
            "review_status": "draft_pending_educator_review",
            "focus": {"subtopic_id": "x", "subtopic_name": "N<script>",
                      "correct": 0, "incorrect": 2, "out_of": 2},
            "action": "Try <b>this</b> action."}],
        "baseline_candidate_id": baseline,
        "selected_candidate_id": selected,
        "template_baseline": {
            "sections": [{"kind": "completion", "text": text}],
            "text": text},
        "message": {
            "sections": [{"kind": "completion", "text": text}],
            "text": text},
        "requires_human_review": True,
        "trace": {
            "policy_version": "observed_subtopic_feedback_v2",
            "selection_source": "rules",
            "phrasing_source": "deterministic",
            "fallback_reason": "bad <i>reason</i>",
            "selection_matches_baseline": True,
            "validation": "deterministic_evidence_and_draft_actions",
            "provider_advantage_demonstrated": False}}


def mini_report():
    return {
        "schema": REPORT_SCHEMA,
        "status": "draft_not_for_learner_delivery",
        "source": {"file": "inputs<x>.json", "sha256": "a" * 64,
                   "bank_sha256": "b" * 64,
                   "taxonomy_sha256": "c" * 64},
        "policy": {"version": "observed_subtopic_feedback_v2",
                   "priority_description": "Draft <heuristic> note.",
                   "provider_mode": "deterministic_no_provider_calls",
                   "source_sha256": "d" * 64},
        "coverage": {"saved_scenarios": 1,
                     "note": "Finite <fixture> coverage."},
        "summary": {"scenario_labels": 1, "feedback_packages": 3,
                    "midpoint_packages": 1, "end_packages": 2,
                    "new_provider_calls": 0,
                    "educator_review_completed": False,
                    "educational_effectiveness_tested": False},
        "scenarios": [{
            "name": "sc<x>enario",
            "group": "g<script>",
            "packages": [
                {"audience": "student", "checkpoint": "midpoint",
                 "review": _review("student", "midpoint",
                                   "Keep <going> when ready.")},
                {"audience": "student", "checkpoint": "end",
                 "review": _review("student", "end",
                                   "Done <script>alert(1)</script>.",
                                   selected="review_x")},
                {"audience": "teacher", "checkpoint": "end",
                 "review": _teacher_review()}]}]}


def _teacher_review():
    """Teacher package whose selected message has no sections list."""
    review = _review("teacher", "end", "Record <b>ready</b>.",
                     selected="review_x")
    review["message"] = {"text": "Selected-only <u>fallback</u> line."}
    return review


class RenderTests(unittest.TestCase):
    def test_escapes_all_dynamic_content(self):
        page = render(mini_report())
        self.assertNotIn("<script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)
        self.assertIn("sc&lt;x&gt;enario", page)
        self.assertIn("g&lt;script&gt;", page)
        self.assertIn("inputs&lt;x&gt;.json", page)
        self.assertIn("N&lt;script&gt;", page)
        self.assertIn("Try &lt;b&gt;this&lt;/b&gt; action.", page)
        self.assertIn("bad &lt;i&gt;reason&lt;/i&gt;", page)
        self.assertIn("Draft &lt;heuristic&gt; note.", page)
        self.assertIn("&lt;going&gt;", page)

    def test_summary_and_trace_rendered_verbatim(self):
        page = render(mini_report())
        for label in ("Scenario labels", "Feedback packages",
                      "Midpoint packages", "End packages",
                      "New provider calls"):
            self.assertIn(label, page)
        self.assertIn("Educator review completed: no", page)
        self.assertIn("Educational effectiveness tested: no", page)
        for field in ("policy_version", "selection_source",
                      "phrasing_source", "fallback_reason",
                      "selection_matches_baseline",
                      "provider_advantage_demonstrated"):
            self.assertIn(field, page)
        self.assertIn("observed_subtopic_feedback_v2", page)
        self.assertIn("draft_not_for_learner_delivery", page)
        self.assertIn("requires human review", page)

    def test_baseline_and_selected_texts_both_shown(self):
        page = render(mini_report())
        self.assertIn("Deterministic baseline", page)
        self.assertIn("Selected draft", page)
        self.assertIn("Done &lt;script&gt;alert(1)&lt;/script&gt;.",
                      page)
        # Sectioned message: text appears once per column, not repeated.
        self.assertEqual(
            page.count("Keep &lt;going&gt; when ready."), 2)
        # Section-less message falls back to its escaped text once.
        self.assertEqual(
            page.count("Selected-only &lt;u&gt;fallback&lt;/u&gt; "
                       "line."), 1)
        self.assertIn("Sanitized review evidence (selector only; "
                      "withheld at midpoint)", page)

    def test_no_external_assets_or_scripts(self):
        page = render(mini_report())
        for marker in ("<script", "src=", "href=", "http://",
                       "https://", "@import", "<link"):
            self.assertNotIn(marker, page)

    def test_summary_numbers_not_recomputed(self):
        report = mini_report()
        report["summary"]["scenario_labels"] = 999
        report["summary"]["feedback_packages"] = -1
        page = render(report)
        self.assertIn("999", page)
        self.assertIn("-1", page)

    def test_optional_pipeline_fields_render_verbatim(self):
        report = mini_report()
        report["pipeline"] = {
            "status": "completed_with_fallbacks",
            "reuse_policy": "identical effective requests share "
                            "captured responses",
            "latency_warning": "reused package wall time is not "
                               "fresh hosted latency"}
        report["diagnostic_provenance"] = {
            "kt": {"file": "kt<x>.json", "sha256": "e" * 64}}
        report["summary"].update({
            "recorded_hosted_requests": 5,
            "recorded_hosted_failures": 3,
            "end_selections_matching_rules": 7,
            "fallback_packages": 3})
        scenario = report["scenarios"][0]
        scenario["pipeline_stages"] = [
            {"stage": "deterministic_scoring",
             "status": "frozen <observed>"}]
        scenario["assessment_graph"] = {"relation": "is_part_of"}
        scenario["research_diagnostics"] = {
            "kt": {"p_correct_before_each_answer": [0.5]},
            "note": "private <em>research</em>"}
        package = scenario["packages"][1]
        package["selector_execution"] = {
            "status": "completed", "reused": True,
            "capture_file": "jev_<tag>.json",
            "original_call_latency_ms": 12.3}
        package["generator_execution"] = {
            "status": "completed", "reused": False,
            "origin": "hosted_replay",
            "metadata": {"status": "completed", "model_version": "m1"},
            "original_call_latency_ms": 45.6}
        package["pipeline_wall_ms"] = 78.9
        page = render(report)
        # Root verbatim JSON details.
        self.assertIn("Pipeline (verbatim from report)", page)
        self.assertIn("completed_with_fallbacks", page)
        self.assertIn("identical effective requests share "
                      "captured responses", page)
        self.assertIn("Diagnostic provenance (verbatim from report)",
                      page)
        self.assertIn("kt&lt;x&gt;.json", page)
        # Optional summary values shown raw.
        for value in ("5", "3", "7"):
            self.assertIn(value, page)
        for label in ("Recorded hosted requests",
                      "Recorded hosted failures",
                      "End selections matching rules baseline",
                      "Packages with fallback"):
            self.assertIn(label, page)
        # Scenario-level research detail, escaped.
        self.assertIn("Private research replay details", page)
        self.assertIn("Pipeline stages", page)
        self.assertIn("frozen &lt;observed&gt;", page)
        self.assertIn("Assessment graph (frozen replay)", page)
        self.assertIn("Research diagnostics (private, not learner "
                      "advice)", page)
        self.assertIn("private &lt;em&gt;research&lt;/em&gt;", page)
        # Package-level execution records and wall time label.
        self.assertIn("Selector execution", page)
        self.assertIn("Generator execution", page)
        self.assertIn("jev_&lt;tag&gt;.json", page)
        self.assertIn("hosted_replay", page)
        self.assertIn("78.9 ms", page)
        self.assertIn("not fresh hosted latency", page)
        # Footer acknowledges private diagnostics honestly.
        flat = " ".join(page.split())
        self.assertIn(
            "Rendered mechanically from a captured report. All feedback "
            "packages require human review. Jev probabilities and "
            "confidence are not used as educational approval. When "
            "included, KT/conformal values are private frozen research "
            "diagnostics, not student feedback.", flat)

    def test_optional_fields_absent_keeps_old_rendering(self):
        page = render(mini_report())
        for label in ("Pipeline (verbatim from report)",
                      "Diagnostic provenance",
                      "Recorded hosted requests",
                      "Selector execution", "Generator execution",
                      "Pipeline stages", "Assessment graph",
                      "Research diagnostics", "Pipeline wall time"):
            self.assertNotIn(label, page)

    def test_wrong_schema_rejected(self):
        for bad in ("not a dict", [], None,
                    {"schema": "wrong"},
                    {**mini_report(), "schema": REPORT_SCHEMA + "x"}):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    render(bad)


class CliTests(unittest.TestCase):
    def test_writes_output_exclusively(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "report.json"
            source.write_text(json.dumps(mini_report()),
                              encoding="utf-8")
            out = Path(tmp) / "review.html"
            main([str(source), "--out", str(out)])
            page = out.read_text(encoding="utf-8")
            self.assertIn("Observed-Evidence Feedback Review", page)
            with self.assertRaises(SystemExit):
                main([str(source), "--out", str(out)])

    def test_rejects_unreadable_input_and_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "report.json"
            source.write_text(json.dumps({"schema": "wrong"}),
                              encoding="utf-8")
            with self.assertRaises(SystemExit):
                main([str(source), "--out", str(Path(tmp) / "o.html")])
            with self.assertRaises(SystemExit):
                main([str(Path(tmp) / "missing.json"), "--out",
                      str(Path(tmp) / "o.html")])


if __name__ == "__main__":
    unittest.main()
