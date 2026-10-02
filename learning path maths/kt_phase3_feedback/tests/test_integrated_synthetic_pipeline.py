"""Software-contract tests for the integrated synthetic pipeline.

Fully offline: fake native providers/openers only; no credentials or
network are ever constructed. These verify frozen-artifact joins,
request identities, capture caching, budgets, and seed validation —
not educational quality.
"""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import integrated_synthetic_pipeline as pipeline
import phase3_paths
from aitta_generator import AittaGenerator
from evidence_providers import EvidenceJevSelector
from jev_selector import QUESTION_ID
from replay_provider_scenarios import CaptureCache
from transport_diagnostics import InstrumentedEvidenceAittaGenerator
from tests.test_aitta_generator import (
    BASE_URL, FAKE_KEY as AITTA_KEY, ok_body as aitta_ok_body)
from tests.test_evidence_providers import (
    opening_payload, selection)
from tests.test_jev_selector import (
    FakeOpener, FakeResponse, ok_body as jev_ok_body)

JEV_KEY = "fake-pipeline-jev-token"
SOURCE = phase3_paths.ARTIFACTS / "assessment_pipeline_20261001.json"
EVIDENCE_INPUTS = (phase3_paths.ARTIFACTS / "evidence_feedback_20261002_v2"
                   / "inputs.json")
ARTIFACTS_READY = all(path.is_file() for path in (
    SOURCE, EVIDENCE_INPUTS, phase3_paths.APPROVED_BANK,
    phase3_paths.ASSESSMENT_TAXONOMY))
requires_artifacts = unittest.skipUnless(
    ARTIFACTS_READY, "frozen source/evidence artifacts unavailable")


def load_bank_taxonomy():
    bank = json.loads(
        phase3_paths.APPROVED_BANK.read_text(encoding="utf-8"))
    taxonomy = json.loads(
        phase3_paths.ASSESSMENT_TAXONOMY.read_text(encoding="utf-8"))
    return bank, taxonomy


def endpoint_hash(generator):
    return hashlib.sha256(
        generator._endpoint.encode("utf-8")).hexdigest()


class EchoContextOpener(FakeOpener):
    """Echoes the legacy wire candidate id with a teacher-safe opening."""

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        body = json.loads(request.data.decode("utf-8"))
        context = json.loads(body["messages"][1]["content"])
        response = FakeResponse(aitta_ok_body(
            opening="The draft is ready for review.",
            candidate=context["selected_candidate"]["candidate_id"]))
        self.responses.append(response)
        return response


class ChoosingJevOpener(FakeOpener):
    """Returns the first permitted candidate from the wire request."""

    def open(self, request, timeout=None):
        self.requests.append(request)
        self.timeouts.append(timeout)
        body = json.loads(request.data.decode("utf-8"))
        choice = next(iter(body["questions"][QUESTION_ID]["criteria"]))
        response = FakeResponse(jev_ok_body(choice=choice))
        self.responses.append(response)
        return response


def hosted_pair(jev_opener=None, aitta_opener=None, budget=10,
                cache_root=None):
    """Cached selector/generator over instrumented fakes."""
    cache = CaptureCache(cache_root, budget)
    jev = EvidenceJevSelector(
        JEV_KEY, timeout=7.5,
        opener=jev_opener if jev_opener is not None
        else ChoosingJevOpener())
    inner = AittaGenerator(
        AITTA_KEY, BASE_URL, timeout=7.5,
        opener=aitta_opener if aitta_opener is not None
        else EchoContextOpener())
    generator = InstrumentedEvidenceAittaGenerator(inner)
    selector = pipeline.CachedReviewSelector(jev, cache)
    opening = pipeline.CachedReviewOpening(
        generator, cache, inner._model, endpoint_hash(inner))
    return selector, opening, cache, inner


@requires_artifacts
class PrepareSnapshotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bank, cls.taxonomy = load_bank_taxonomy()

    def snapshot(self, names=None):
        return pipeline.prepare_snapshot(
            SOURCE, EVIDENCE_INPUTS, self.bank, self.taxonomy,
            names if names is not None else [pipeline.DEFAULT_SCENARIO])

    def test_all_skills_equal_retains_frozen_diagnostics(self):
        snapshot = self.snapshot()
        self.assertEqual(snapshot["schema"], pipeline.PIPELINE_SCHEMA)
        self.assertEqual(len(snapshot["scenarios"]), 1)
        case = snapshot["scenarios"][0]
        self.assertEqual(case["name"], "all_skills_equal")
        saved = {s["name"]: s for s in
                 json.loads(SOURCE.read_text(encoding="utf-8"))
                 ["scenarios"]}["all_skills_equal"]
        self.assertEqual(
            case["research_diagnostics"]["kt"]
            ["p_correct_before_each_answer"],
            saved["kt_pre_answer_probability"])
        self.assertEqual(
            case["research_diagnostics"]["conformal"],
            saved["conformal"])
        self.assertEqual(case["assessment_graph"], saved["graph"])
        self.assertTrue(snapshot["diagnostic_provenance"])

    def tampered_pair(self, directory, mutate_source=None,
                      mutate_inputs=None):
        original = json.loads(SOURCE.read_text(encoding="utf-8"))
        if mutate_source is not None:
            mutate_source(original)
        source_path = directory / "source.json"
        source_path.write_text(json.dumps(original), encoding="utf-8")
        frozen = json.loads(
            EVIDENCE_INPUTS.read_text(encoding="utf-8"))
        if mutate_inputs is not None:
            mutate_inputs(frozen)
        frozen["source"]["sha256"] = hashlib.sha256(
            source_path.read_bytes()).hexdigest()
        inputs_path = directory / "inputs.json"
        inputs_path.write_text(json.dumps(frozen), encoding="utf-8")
        return source_path, inputs_path

    def test_source_mismatch_rejects(self):
        with self.assertRaises(ValueError):
            pipeline.prepare_snapshot(
                EVIDENCE_INPUTS, EVIDENCE_INPUTS, self.bank,
                self.taxonomy, [pipeline.DEFAULT_SCENARIO])

    def test_policy_mismatch_rejects(self):
        with tempfile.TemporaryDirectory() as raw:
            source_path, inputs_path = self.tampered_pair(
                Path(raw),
                mutate_inputs=lambda frozen: frozen["policy"]
                .__setitem__("version", "tampered_policy"))
            with self.assertRaises(ValueError):
                pipeline.prepare_snapshot(
                    source_path, inputs_path, self.bank, self.taxonomy,
                    [pipeline.DEFAULT_SCENARIO])

    def test_graph_mismatch_rejects(self):
        def corrupt(original):
            case = next(s for s in original["scenarios"]
                        if s["name"] == "all_skills_equal")
            case["graph"]["checkpoints"]["end"]["topics"][0][
                "correct"] += 1

        with tempfile.TemporaryDirectory() as raw:
            source_path, inputs_path = self.tampered_pair(
                Path(raw), mutate_source=corrupt)
            with self.assertRaises(ValueError):
                pipeline.prepare_snapshot(
                    source_path, inputs_path, self.bank, self.taxonomy,
                    [pipeline.DEFAULT_SCENARIO])

    def test_conformal_item_probability_mismatch_rejects(self):
        def corrupt(original):
            case = next(s for s in original["scenarios"]
                        if s["name"] == "all_skills_equal")
            rows = case["conformal"]["checkpoints"]["end"]
            decision = rows[next(iter(rows))]["item_decisions"][0]
            decision["p_correct"] = (
                0.5 if decision["p_correct"] != 0.5 else 0.25)

        with tempfile.TemporaryDirectory() as raw:
            source_path, inputs_path = self.tampered_pair(
                Path(raw), mutate_source=corrupt)
            with self.assertRaises(ValueError):
                pipeline.prepare_snapshot(
                    source_path, inputs_path, self.bank, self.taxonomy,
                    [pipeline.DEFAULT_SCENARIO])

    def test_unknown_scenario_rejects(self):
        with self.assertRaises(ValueError):
            self.snapshot(["no_such_scenario"])


class WireIdentityTests(unittest.TestCase):
    def test_jev_wire_body_matches_request_identity(self):
        payload = selection("student", "end")
        opener = FakeOpener(jev_ok_body(choice="review_sub_sA"))
        selector = EvidenceJevSelector(JEV_KEY, opener=opener)
        result = selector.select(payload)
        self.assertEqual(result, {"candidate_id": "review_sub_sA"})
        request = pipeline.jev_request(payload)
        self.assertEqual(len(opener.requests), 1)
        sent = opener.requests[0]
        self.assertEqual(sent.full_url, request["endpoint"])
        self.assertEqual(json.loads(sent.data.decode("utf-8")),
                         request["body"])

    def test_aitta_wire_body_matches_request_identity(self):
        inner = AittaGenerator(AITTA_KEY, BASE_URL,
                               opener=FakeOpener(
                                   aitta_ok_body(
                                       candidate="observed_summary")))
        generator = InstrumentedEvidenceAittaGenerator(inner)
        payload = opening_payload()
        result = generator.generate(payload)
        self.assertEqual(result["candidate_id"], "review_sub_sA")
        expected = pipeline.aitta_request(
            payload, inner._model, endpoint_hash(inner))
        opener = inner._opener._opener
        sent = opener.requests[0]
        self.assertEqual(
            hashlib.sha256(
                sent.full_url.encode("utf-8")).hexdigest(),
            expected["endpoint_sha256"])
        self.assertEqual(json.loads(sent.data.decode("utf-8")),
                         expected["body"])

    def test_wires_carry_no_diagnostics_or_answers(self):
        jev_opener = FakeOpener(jev_ok_body(choice="review_sub_sA"))
        EvidenceJevSelector(JEV_KEY, opener=jev_opener).select(
            selection("teacher", "end"))
        inner = AittaGenerator(AITTA_KEY, BASE_URL,
                               opener=FakeOpener(
                                   aitta_ok_body(
                                       candidate="observed_summary")))
        InstrumentedEvidenceAittaGenerator(inner).generate(
            opening_payload())
        jev_blob = jev_opener.requests[0].data.decode("utf-8")
        for marker in ("question_id", "answer_index",
                       "selected_index", "options", "p_correct",
                       "conformal", "kt_", "theta"):
            self.assertNotIn(marker, jev_blob)
        aitta_blob = inner._opener._opener.requests[0].data.decode(
            "utf-8")
        for marker in ("question_id", "answer_index",
                       "selected_index", "options", "p_correct",
                       "conformal", "kt_", "theta", "evidence",
                       "subtopic", "out_of", "bank_sha256",
                       "review_sub_sA"):
            self.assertNotIn(marker, aitta_blob)


@requires_artifacts
class MockedSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bank, cls.taxonomy = load_bank_taxonomy()

    def test_all_three_packages_complete_within_five_requests(self):
        snapshot = pipeline.prepare_snapshot(
            SOURCE, EVIDENCE_INPUTS, self.bank, self.taxonomy,
            [pipeline.DEFAULT_SCENARIO])
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            cache_root = output / "provider_captures"
            cache_root.mkdir()
            selector, opening, cache, inner = hosted_pair(
                cache_root=cache_root)
            contract = pipeline._contract(
                snapshot, "hosted", inner._model, endpoint_hash(inner),
                7.5)
            report = pipeline.replay(
                snapshot, output, contract, selector, opening)
            captures = list(cache_root.glob("*.json"))
            self.assertLessEqual(len(captures), 5)
            self.assertLessEqual(cache.new_calls, 5)
            self.assertEqual(len(report["scenarios"]), 1)
            scenario = report["scenarios"][0]
            self.assertEqual(len(scenario["packages"]), 3)
            self.assertEqual(
                report["summary"]["recorded_hosted_requests"],
                len(captures))
            self.assertEqual(report["summary"]
                             ["recorded_hosted_failures"], 0)
            self.assertEqual(report["summary"]["fallback_packages"], 0)
            case = snapshot["scenarios"][0]
            for package in scenario["packages"]:
                evidence = case["evidence"][package["checkpoint"]]
                review = package["review"]
                if package["checkpoint"] == "midpoint":
                    self.assertEqual(review["sanitized_evidence"], {})
                else:
                    self.assertEqual(review["sanitized_evidence"],
                                     evidence)
                for field in ("selector_execution",
                              "generator_execution",
                              "pipeline_wall_ms"):
                    self.assertIn(field, package)
                self.assertIsNotNone(package["generator_execution"])
            end_packages = [p for p in scenario["packages"]
                            if p["checkpoint"] == "end"]
            for package in end_packages:
                self.assertEqual(package["selector_execution"]
                                 ["metadata"]["status"], "completed")
                self.assertEqual(package["generator_execution"]
                                 ["metadata"]["status"], "completed")
            self.assertEqual(
                scenario["packages"][0]["selector_execution"],
                {"status": "local_single_candidate", "reused": False})


@requires_artifacts
class BudgetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bank, cls.taxonomy = load_bank_taxonomy()

    def test_budget_two_exits_before_third_request(self):
        snapshot = pipeline.prepare_snapshot(
            SOURCE, EVIDENCE_INPUTS, self.bank, self.taxonomy,
            [pipeline.DEFAULT_SCENARIO])
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw)
            cache_root = output / "provider_captures"
            cache_root.mkdir()
            selector, opening, cache, inner = hosted_pair(
                budget=2, cache_root=cache_root)
            contract = pipeline._contract(
                snapshot, "hosted", inner._model, endpoint_hash(inner),
                7.5)
            with self.assertRaises(SystemExit):
                pipeline.replay(
                    snapshot, output, contract, selector, opening)
            self.assertEqual(len(list(cache_root.glob("*.json"))), 2)
            completed = (output / "packages" / "000_0.json")
            self.assertTrue(completed.is_file())
            self.assertFalse(
                (output / "packages" / "000_1.json").exists())


class AittaReuseTests(unittest.TestCase):
    def test_alternate_focus_ids_reuse_opening_return_current_id(self):
        with tempfile.TemporaryDirectory() as raw:
            selector, opening, cache, inner = hosted_pair(
                cache_root=Path(raw))
            first = opening_payload(candidate_id="review_sub_sA",
                                    strategy="focused_review")
            second = opening_payload(candidate_id="review_sub_sB",
                                     strategy="supported_review")
            one = opening.generate(first)
            two = opening.generate(second)
            self.assertEqual(one["candidate_id"], "review_sub_sA")
            self.assertEqual(two["candidate_id"], "review_sub_sB")
            self.assertEqual(one["opening"], two["opening"])
            self.assertEqual(cache.new_calls, 1)
            self.assertEqual(len(list(Path(raw).glob("*.json"))), 1)
            self.assertTrue(opening.execution["reused"])


class FailureCacheTests(unittest.TestCase):
    def test_captured_failure_is_not_retried(self):
        with tempfile.TemporaryDirectory() as raw:
            opener = FakeOpener(error=TimeoutError("once"))
            _, opening, cache, _ = hosted_pair(
                aitta_opener=opener, cache_root=Path(raw))
            payload = opening_payload()
            with self.assertRaisesRegex(RuntimeError,
                                        "captured_generator_failure"):
                opening.generate(payload)
            with self.assertRaisesRegex(RuntimeError,
                                        "captured_generator_failure"):
                opening.generate(payload)
            self.assertEqual(len(opener.requests), 1)
            self.assertEqual(cache.new_calls, 1)
            capture = json.loads(
                next(Path(raw).glob("*.json")).read_text(
                    encoding="utf-8"))
            self.assertFalse(capture["ok"])
            self.assertIsNone(capture["reply"])


class SeedSuccessesTests(unittest.TestCase):
    MODEL = "fake-aitta-model"
    HASH = hashlib.sha256(b"https://aitta.example/v1/"
                         b"chat/completions").hexdigest()

    def record(self, provider, request, reply, ok=True):
        return {"provider": provider, "request": request,
                "ok": ok, "reply": reply,
                "metadata": {"status": "completed" if ok else "failed",
                             "model_version": self.MODEL,
                             "usage": None},
                "original_call_latency_ms": 1.0}

    def jev_record(self):
        payload = selection("student", "end")
        request = pipeline.jev_request(payload)
        choice = request["body"]["state"]["candidates"][0][
            "candidate_id"]
        return self.record("jev", request, {"candidate_id": choice})

    def aitta_record(self, checkpoint="end"):
        stub = opening_payload(
            checkpoint=checkpoint,
            candidate_id=("neutral" if checkpoint == "midpoint"
                          else "optional_consolidation"),
            strategy=("neutral_encouragement"
                      if checkpoint == "midpoint"
                      else "optional_consolidation"))
        request = pipeline.aitta_request(stub, self.MODEL, self.HASH)
        return self.record("aitta", request,
                           {"opening": "Thank you for completing."})

    def seed_dir(self, parent, records):
        directory = parent / "seeds"
        directory.mkdir()
        for index, record in enumerate(records):
            (directory / f"{index}.json").write_text(
                json.dumps(record), encoding="utf-8")
        return directory

    def test_seeds_only_valid_successful_current_requests(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            failed = self.aitta_record("midpoint")
            failed["ok"] = False
            failed["reply"] = None
            failed["metadata"]["status"] = "failed"
            directory = self.seed_dir(
                root, [self.jev_record(), self.aitta_record(), failed])
            cache_root = root / "captures"
            cache_root.mkdir()
            cache = CaptureCache(cache_root, 10)
            seeded = pipeline.seed_successes(
                cache, directory, self.MODEL, self.HASH)
            self.assertEqual(seeded, 2)
            captures = list(cache_root.glob("*.json"))
            self.assertEqual(len(captures), 2)
            # The failed capture was skipped, not written.
            self.assertFalse(
                cache.path("aitta", failed["request"]).exists())
            # A reused seed satisfies obtain without any callback.
            request = pipeline.jev_request(selection("student", "end"))
            reply, execution = cache.obtain(
                "jev", request,
                lambda: self.fail("seeded capture re-requested"),
                mock.Mock(last_metadata={}))
            self.assertEqual(reply["candidate_id"],
                             request["body"]["state"]["candidates"][0]
                             ["candidate_id"])
            self.assertTrue(execution["reused"])

    def test_corrupted_seed_request_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            record = self.jev_record()
            record["request"]["body"]["questions"][QUESTION_ID][
                "criteria"]["forged_candidate"] = "forged action"
            directory = self.seed_dir(root, [record])
            cache = CaptureCache(root, 10)
            with self.assertRaises(ValueError):
                pipeline.seed_successes(
                    cache, directory, self.MODEL, self.HASH)

    def test_seed_reply_must_be_permitted_candidate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            record = self.jev_record()
            record["reply"] = {"candidate_id": "invented_focus"}
            directory = self.seed_dir(root, [record])
            cache = CaptureCache(root, 10)
            with self.assertRaises(ValueError):
                pipeline.seed_successes(
                    cache, directory, self.MODEL, self.HASH)


class MainPreflightTests(unittest.TestCase):
    def test_report_no_overwrite_checked_before_credentials(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "out"
            output.mkdir()
            (output / "report.json").write_text(
                "{}", encoding="utf-8")
            blocker = mock.Mock(
                side_effect=AssertionError("credentials touched"))
            argv = ["--mode", "hosted", "--out-dir", str(output),
                    "--jev-env-file", str(Path(raw) / "key.env"),
                    "--max-new-calls", "1"]
            with mock.patch.object(
                    pipeline, "_read_env_file", blocker):
                with self.assertRaises(SystemExit):
                    pipeline.main(argv)
            blocker.assert_not_called()

    def test_hosted_requires_key_file_and_budget(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "out"
            for argv in (
                    ["--mode", "hosted", "--out-dir", str(output)],
                    ["--mode", "hosted", "--out-dir", str(output),
                     "--jev-env-file", str(Path(raw) / "key.env")],
                    ["--mode", "hosted", "--out-dir", str(output),
                     "--max-new-calls", "3"]):
                with self.subTest(argv=argv):
                    with self.assertRaises(SystemExit):
                        pipeline.main(argv)

    @requires_artifacts
    def test_offline_mode_needs_no_native_providers(self):
        with tempfile.TemporaryDirectory() as raw:
            output = Path(raw) / "out"
            fake_aitta = mock.Mock()
            fake_aitta.from_env.side_effect = AssertionError(
                "provider constructed in offline mode")
            with mock.patch.object(
                    pipeline, "AittaGenerator", fake_aitta):
                pipeline.main(["--out-dir", str(output)])
            fake_aitta.from_env.assert_not_called()
            report = json.loads(
                (output / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["summary"]
                             ["recorded_hosted_requests"], 0)
            self.assertEqual(len(report["scenarios"]), 1)
            for package in report["scenarios"][0]["packages"]:
                self.assertEqual(
                    package["selector_execution"]["status"],
                    "offline_rules_no_provider")
            self.assertTrue((output / "inputs.json").is_file())
            self.assertTrue((output / "contract.json").is_file())
            self.assertTrue((output / "request_plan.json").is_file())


if __name__ == "__main__":
    unittest.main()
