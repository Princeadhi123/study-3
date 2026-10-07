"""Offline warm replay and Jev contract checks, not provider-quality scores."""
import contextlib
import copy
import io
import json
import secrets
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path

import phase3_paths
from demo_service import DemoService
from evidence_feedback import (
    build_research_evidence, run_feedback, selection_payload)
from evidence_feedback_policy import PRACTICE_ACTIONS
from feedback_service import assessment_feedback_graph
from live_diagnostics import LiveDiagnostics
from research_runtime import load_research_bank
from research_workspace import case_display
from tests.helpers import make_bank, make_taxonomy
from tests.test_evidence_providers import jev
from tests.test_jev_selector import FakeOpener, ok_body
from tests.test_kt_adapter import FakeKT
from tests.test_scenario_replays import replay_fixture
from warm_scenarios import write_capture


@unittest.skipUnless(
    (phase3_paths.ARTIFACTS / "bounded_content_graph_v3_20261007/manifest.json").exists(),
    "private v3 content capture not installed")
class WarmScenarioTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.capture_tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.capture_tmp.cleanup)
        cls.bank, cls.taxonomy, cls.provenance = load_research_bank("warm")
        diagnostics = LiveDiagnostics(model_loader=lambda: FakeKT(cls.bank))
        with contextlib.redirect_stdout(io.StringIO()):
            cls.source_path, cls.report_path = write_capture(
                Path(cls.capture_tmp.name) / "capture", diagnostics)
        cls.source = json.loads(cls.source_path.read_text(encoding="utf-8"))
        cls.report = json.loads(cls.report_path.read_text(encoding="utf-8"))

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.demo = make_bank()

    def service(self, **kwargs):
        service = DemoService(
            root=Path(self.tmp.name) / "runtime", bank=self.demo,
            taxonomy=make_taxonomy(self.demo),
            diagnostics=LiveDiagnostics(model_loader=lambda: FakeKT(self.bank)),
            replay_report=self.report_path, replay_source=self.source_path, **kwargs)
        self.addCleanup(service.close, wait=True)
        return service

    def wait_run(self, service, run_id):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            run = service.replays.get(run_id)
            if run["status"] not in ("queued", "running", "cancelling"):
                return run
            time.sleep(.03)
        self.fail("warm replay did not finish")

    def test_capture_has_exact_54_bank_bound_cases_and_fixed_answers(self):
        source = self.source
        self.assertEqual(source["bank_mode"], "warm")
        self.assertEqual(source["bank_provenance"], self.provenance)
        self.assertEqual(len(source["scenarios"]), 54)
        self.assertEqual(len({c["name"] for c in source["scenarios"]}), 54)
        self.assertEqual(Counter(c["group"] for c in source["scenarios"]),
                         {"deterministic": 20, "seeded": 30, "subtopic": 2, "distractor": 2})
        ids = [q["question_id"] for q in self.bank["questions"]]
        for case in source["scenarios"]:
            self.assertEqual([row["question_id"] for row in case["responses"]], ids)
            self.assertEqual([row["correct"] for row in case["responses"]],
                             [row["selected_index"] == q["answer_index"]
                              for row, q in zip(case["responses"], self.bank["questions"])])
        fractions = next(sid for sid, name in self.bank["skill_names"].items()
                         if name == "Murtolukujen kerto- ja jakolasku")
        for case in source["scenarios"]:
            if case["profile"] == "weak_fractions":
                self.assertEqual({row["true_probability"] for row in case["responses"]
                                  if row["skill_id"] == fractions}, {.2})
        distractors = [c for c in source["scenarios"] if c["group"] == "distractor"]
        self.assertEqual([r["correct"] for r in distractors[0]["responses"]],
                         [r["correct"] for r in distractors[1]["responses"]])
        self.assertNotEqual([r["selected_index"] for r in distractors[0]["responses"]],
                            [r["selected_index"] for r in distractors[1]["responses"]])
        for case in self.report["scenarios"]:
            self.assertEqual(len(case["packages"]), 3)
            self.assertEqual(len(case["research_diagnostics"]["kt"]["p_correct_before_each_answer"]), 40)
        self.assertNotIn("conformal", json.dumps(self.report).lower())
        self.assertFalse(self.report["educator_approved"])
        with self.assertRaises(FileExistsError):
            write_capture(self.source_path.parent)

    def test_all_warm_replays_complete_using_warm_not_demo_bank(self):
        service = self.service(replay_bank_mode="warm")
        library = service.research.library()
        self.assertEqual(library["bank_mode"], "warm")
        self.assertEqual(library["bank_label"], "Warm research bank")
        self.assertEqual(library["summary"]["scenarios"], 54)
        before = self.source_path.read_bytes(), self.report_path.read_bytes()
        request = {"request_id": secrets.token_hex(16),
                   "report_sha256": library["report_sha256"],
                   "scenario_ids": [row["id"] for row in library["scenarios"]],
                   "provider_mode": "rules", "allow_provider_calls": False}
        run = self.wait_run(service, service.replays.start(request)["id"])
        self.assertEqual(run["status"], "complete")
        self.assertEqual(run["bank_mode"], "warm")
        self.assertEqual(run["completed"], 54)
        self.assertEqual(run["failed"], 0)
        for row in run["cases"]:
            self.assertEqual(row["changes"]["observed_correct_delta"], 0)
            detail = service.replays.result(run["id"], row["scenario_id"])
            self.assertEqual(detail["bank_mode"], "warm")
            self.assertEqual([q["question_id"] for q in detail["question_responses"]],
                             [q["question_id"] for q in self.bank["questions"]])
            self.assertFalse(detail["provenance"]["educator_approved"])
        retained = service.scenario_detail(run["cases"][0]["scenario_id"])["detail"]
        self.assertEqual(retained["bank_mode"], "warm")
        self.assertEqual(len(retained["question_responses"]), 40)
        self.assertEqual(retained["content_context"]["practice_question_count"], 24)
        self.assertEqual(before, (self.source_path.read_bytes(), self.report_path.read_bytes()))
        self.assertEqual(service.list_sessions(), [])

    def test_warm_source_cannot_be_replayed_as_demo(self):
        service = self.service()
        with self.assertRaisesRegex(ValueError, "incompatible"):
            service.replays.source_cases()

    def test_warm_and_demo_replay_history_remain_separate_in_shared_root(self):
        warm = self.service(replay_bank_mode="warm")
        record = {"id": secrets.token_hex(16), "created_at": "2026-10-07T00:00:00Z",
                  "bank_mode": "warm", "status": "complete", "cases": []}
        warm.replays._save(record)
        warm.close(wait=True)
        source, report, _ = replay_fixture(Path(self.tmp.name), self.demo, make_taxonomy(self.demo))
        demo = DemoService(root=Path(self.tmp.name) / "runtime",
                           bank=self.demo, taxonomy=make_taxonomy(self.demo),
                           diagnostics=LiveDiagnostics(model_loader=lambda: FakeKT(self.demo)),
                           replay_report=report, replay_source=source)
        self.addCleanup(demo.close, wait=True)
        self.assertEqual(demo.replays.list()["runs"], [])
        self.assertEqual(demo.replays.get(record["id"])["bank_mode"], "warm")

    def test_jev_warm_wire_contains_only_permitted_observed_evidence(self):
        case = next(c for c in self.source["scenarios"] if c["name"] == "all_incorrect")
        rows = [{key: row[key] for key in ("question_id", "selected_index")}
                for row in case["responses"]]
        evidence = build_research_evidence(self.bank, self.taxonomy, rows)
        for audience in ("student", "teacher"):
            payload = selection_payload(evidence, audience, "end")
            self.assertGreater(len(payload["candidates"]), 1)
            choice = payload["candidates"][-1]["candidate_id"]
            opener = FakeOpener(ok_body(choice=choice))
            self.assertEqual(jev(opener).select(payload), {"candidate_id": choice})
            wire = json.loads(opener.requests[0].data)
            self.assertEqual(wire["state"], {
                "audience": audience, "checkpoint": "end",
                "evidence": payload["evidence"], "candidates": payload["candidates"]})
            blob = json.dumps(wire)
            for marker in ("question_id", "item_id", "content_text", "answer_index",
                           "p_correct", "conformal", "support_link", "possible_error"):
                self.assertNotIn(marker, blob)
            for question in self.bank["questions"]:
                self.assertNotIn(question["question_id"], blob)
                self.assertNotIn(question["item_id"], blob)
            for field in ("kt", "graph", "possible_error"):
                bad = copy.deepcopy(payload)
                bad[field] = {"private": True}
                with self.assertRaises(ValueError):
                    jev(opener).select(bad)
            self.assertEqual(len(opener.requests), 1)
        graph = assessment_feedback_graph(self.bank, self.taxonomy, rows)
        self.assertIn("pending_educator_review", graph["scope"])
        label = case_display("multiple_weak", evidence)["display_name"]
        self.assertIn("Percentages", label)
        self.assertNotIn("Arithmetic", label)

    def test_warm_feedback_plan_uses_assessed_concepts_and_actions(self):
        # Incorrect answers on one assessed graph concept; the plan must
        # carry that concept's fixed action and recomputed parent counts.
        target = "combine_like_terms"
        topic = next(t for t in self.taxonomy["topics"]
                     if any(s["id"] == target for s in t["subtopics"]))
        subtopic = next(s for s in topic["subtopics"]
                        if s["id"] == target)
        concept_qids = set(subtopic["question_ids"])
        distractor_qid = next(
            q["question_id"] for q in self.bank["questions"]
            if q["skill_id"] != topic["skill_id"])
        rows = []
        for question in self.bank["questions"]:
            index = question["answer_index"]
            if (question["question_id"] in concept_qids
                    or question["question_id"] == distractor_qid):
                index = (index + 1) % len(question["options"])
            rows.append({"question_id": question["question_id"],
                         "selected_index": index})
        evidence = build_research_evidence(
            self.bank, self.taxonomy, rows)
        review = run_feedback(evidence, "student", "end")
        plan = review["feedback_plan"]
        self.assertEqual(plan["schema"],
                         "phase3_observed_feedback_plan_v1")
        self.assertIs(plan["kt_used"], False)
        self.assertEqual(plan["candidate_id"], "review_" + target)
        self.assertEqual(plan["focus"]["subtopic_id"], target)
        # All of the concept's questions were missed, so the authored
        # action is wrapped by the supported-review teacher prefix.
        self.assertEqual(plan["strategy"], "supported_review")
        self.assertTrue(plan["action"].startswith(
            "Start with a teacher"))
        self.assertTrue(plan["action"].endswith(PRACTICE_ACTIONS[target]))
        parent = next(s for s in evidence["skills"]
                      if s["skill_id"] == topic["skill_id"])
        self.assertEqual(
            plan["planning"]["parent_counts"],
            {key: parent[key]
             for key in ("correct", "incorrect", "out_of")})
        self.assertEqual(plan["planning"]["parent_counts"],
                         {"correct": 4, "incorrect": 6, "out_of": 10})
        self.assertEqual(plan["planning"]["error_pattern"],
                         "multiple_incorrect_answers"
                         if len(concept_qids) > 1
                         else "isolated_incorrect_answer")
        teacher = run_feedback(evidence, "teacher", "end")
        teacher_text = teacher["message"]["text"]
        self.assertIn(
            "Within Combining like terms, 4 of 10 answers were "
            "correct and 6 were incorrect.", teacher_text)
        self.assertNotIn("Samanmuotoisten", teacher_text)
        # The validated selection wire carries the same planning block
        # and nothing raw, private, or diagnostic.
        payload = selection_payload(evidence, "student", "end")
        opener = FakeOpener(ok_body(choice="review_" + target))
        self.assertEqual(jev(opener).select(payload),
                         {"candidate_id": "review_" + target})
        wire = json.loads(opener.requests[0].data)
        candidate = next(c for c in wire["state"]["candidates"]
                         if c["candidate_id"] == "review_" + target)
        self.assertEqual(candidate["planning"], plan["planning"])
        blob = opener.requests[0].data.decode("utf-8")
        for marker in ("question_id", "item_id", "content_text",
                       "answer_index", "p_correct", "conformal",
                       "support_link", "possible_error", "kt_"):
            self.assertNotIn(marker, blob)
        for question in self.bank["questions"]:
            self.assertNotIn(question["question_id"], blob)
            self.assertNotIn(question["item_id"], blob)
            self.assertNotIn(question["text"], blob)
