"""Software checks for replay accounting; no hosted calls or quality scores."""
import copy
import json
import tempfile
import unittest
from pathlib import Path

from aitta_generator import AittaGenerator
from jev_selector import JevSelector
from replay_provider_scenarios import (
    CachedGenerator, CachedSelector, CaptureCache, SKILL_ALIASES,
    evidence_for, freeze, generation_request)
from synthetic_feedback import run_synthetic_feedback
from tests.test_aitta_generator import ContextOpener, generation_payload
from tests.test_jev_selector import FakeOpener, ok_body


def synthetic_source():
    ids = list(SKILL_ALIASES)
    rows = [{"position": i + 1, "question_id": f"question_{i}",
             "skill_id": ids[i % 4], "correct": True} for i in range(40)]
    observed = {}
    for checkpoint, denominator in (("midpoint", 5), ("end", 10)):
        feed = {"skills": [
            {"skill_id": sid, "skill_name": SKILL_ALIASES[sid][2],
             "correct": denominator, "out_of": denominator}
            for sid in ids]}
        if checkpoint == "end":
            feed["total"] = {"correct": 40, "out_of": 40}
        observed[checkpoint] = feed
    cases = [{"name": f"case_{i}", "group": "deterministic", "profile": None,
              "seed": 42, "responses": copy.deepcopy(rows),
              "observed": copy.deepcopy(observed)} for i in range(54)]
    return {"schema": "phase3_fixed40_comparison_v1",
            "scope": "private_synthetic_fixed_40_cold_start_not_student_validation",
            "scenarios": cases}


class ProviderReplayTests(unittest.TestCase):
    def freeze_fixture(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "source.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            return freeze(path)

    def test_snapshot_allowlist_and_count_validation(self):
        snapshot = self.freeze_fixture(synthetic_source())
        self.assertEqual(len(snapshot["cases"]), 54)
        blob = json.dumps(snapshot)
        for forbidden in ("question_id", "responses", "selected_index", "kt_"):
            self.assertNotIn(forbidden, blob)
        self.assertEqual(snapshot["cases"][0]["skills"]["end"][0], {
            "skill_id": "skill_a", "skill_name": "Arithmetic",
            "correct": 10, "out_of": 10})
        for field, value in (("correct", 9), ("out_of", True)):
            bad = synthetic_source()
            bad["scenarios"][0]["observed"]["end"]["skills"][0][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.freeze_fixture(bad)

    def test_repeated_cases_reuse_effective_requests(self):
        snapshot = self.freeze_fixture(synthetic_source())
        jev_opener = FakeOpener(ok_body())
        aitta_opener = ContextOpener(b"")
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), 5)
            selector = CachedSelector(
                JevSelector("fake-jev-token", opener=jev_opener), cache)
            generator = CachedGenerator(AittaGenerator(
                "fake-aitta-token", "https://aitta.example/v1",
                opener=aitta_opener), cache)
            results = []
            for case in snapshot["cases"]:
                for audience, checkpoint in (
                        ("student", "midpoint"), ("student", "end"), ("teacher", "end")):
                    results.append(run_synthetic_feedback(
                        evidence_for(case, audience, checkpoint), selector, generator))
            self.assertEqual(len(results), 162)
            self.assertEqual(len(jev_opener.requests), 2)
            self.assertEqual(len(aitta_opener.requests), 3)
            self.assertEqual(cache.new_calls, 5)
            self.assertTrue(all(r["trace"]["fallback_reason"] is None for r in results))
            self.assertEqual(len(list(Path(tmp).glob("*.json"))), 5)

    def test_failures_are_captured_not_retried(self):
        snapshot = self.freeze_fixture(synthetic_source())
        opener = FakeOpener(error=TimeoutError("secret-fake-token"))
        with tempfile.TemporaryDirectory() as tmp:
            cache = CaptureCache(Path(tmp), 1)
            selector = CachedSelector(
                JevSelector("fake-key", opener=opener), cache)
            evidence = evidence_for(snapshot["cases"][0], "student", "end")
            first = run_synthetic_feedback(evidence, selector)
            second = run_synthetic_feedback(evidence, selector)
            self.assertEqual(first["trace"]["fallback_reason"], "selector_error")
            self.assertEqual(second["trace"]["fallback_reason"], "selector_error")
            self.assertEqual(len(opener.requests), 1)
            self.assertTrue(selector.execution["reused"])
            captured = next(Path(tmp).glob("*.json")).read_text()
            self.assertNotIn("secret-fake-token", captured)

    def test_aitta_request_identity_excludes_counts(self):
        first = generation_payload("student", "end")
        second = copy.deepcopy(first)
        second["evidence"]["skills"][0]["correct"] = 8
        second["evidence"]["total"]["correct"] -= 1
        self.assertEqual(generation_request(first, "openai/gpt-oss-120b"),
                         generation_request(second, "openai/gpt-oss-120b"))
        self.assertNotEqual(generation_request(first, "model_a"),
                            generation_request(first, "model_b"))

    def test_budget_exhaustion_does_not_become_fallback(self):
        snapshot = self.freeze_fixture(synthetic_source())
        opener = FakeOpener(ok_body())
        with tempfile.TemporaryDirectory() as tmp:
            selector = CachedSelector(
                JevSelector("fake-key", opener=opener), CaptureCache(Path(tmp), 0))
            with self.assertRaises(SystemExit):
                run_synthetic_feedback(
                    evidence_for(snapshot["cases"][0], "student", "end"), selector)
            self.assertEqual(opener.requests, [])


if __name__ == "__main__":
    unittest.main()
