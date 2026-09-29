import copy
import json
import tempfile
import unittest
from pathlib import Path

import torch

from mcq_service import MCQSessionService
from research_matrix import reordered_bank, run_matrix, unknown_item_bank
from scenario_report import compile_matrix_taxonomy, write_matrix_taxonomy
from session_store import SessionStore, bank_fingerprint
from tests.helpers import make_bank, make_taxonomy
from tests.test_kt_adapter import FakeKT


class HistoryKT(FakeKT):
    def probs(self, batch):
        self.batch = batch
        n = batch["correct"].shape[1]
        if n == 60:
            p = .75 if batch["correct"][0, :20].mean() > .5 else .25
            return torch.full((1, n), p)
        return torch.linspace(.1, .9, n).unsqueeze(0)


class Gate:
    def __init__(self, k):
        self.k = k

    def item_decision(self, probability, regime):
        return type("Item", (), {"prediction_set": (0, 1),
                                 "status": type("Status", (), {"value": "UNCERTAIN_BEHAVIOR"})()})()

    def checkpoint(self, probabilities, sid, regime):
        return type("Decision", (), {"to_dict": lambda self: {
            "n_items": len(probabilities), "regime": regime,
            "status": "UNCERTAIN_BEHAVIOR", "lower": 0.1, "upper": 0.9}})()


class ResearchMatrixTests(unittest.TestCase):
    def setUp(self):
        self.bank = make_bank()
        self.kt = HistoryKT(self.bank)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = SessionStore(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def matrix(self, graph=None, taxonomy=None):
        return run_matrix(self.bank, {"profile": "all_correct", "seed": 11},
                          self.store, self.kt, Gate(10), Gate(5), graph=graph,
                          order_seed=17, taxonomy=taxonomy)

    def test_paired_order_preserves_items_answers_and_observed_counts(self):
        result = self.matrix()
        self.assertEqual(result["scope"], "researcher_only")
        self.assertEqual(len(result["variants"]), 4)
        baseline = result["baseline"]
        order = result["variants"][0]
        self.assertEqual(order["kind"], "within_half_order")
        self.assertNotEqual(order["research_bank_sha256"], result["base_bank_sha256"])
        original = [r["question_id"] for r in baseline["responses"]]
        self.assertNotEqual(order["question_order"], original)
        self.assertEqual(set(order["question_order"][:20]), set(original[:20]))
        self.assertEqual(set(order["question_order"][20:]), set(original[20:]))
        self.assertTrue(all(v["observed_counts_unchanged"] for v in result["variants"]))
        self.assertEqual(baseline["observed"]["end"]["teacher"]["total"],
                         {"correct": 40, "out_of": 40})
        self.assertEqual(5, order["conformal"]["midpoint_calibrated_k"])
        self.assertEqual(10, order["conformal"]["end_calibrated_k"])
        self.assertNotIn("conformal", json.dumps(baseline["observed"]["end"]["student"]))

    def test_research_bank_variants_cannot_be_student_served(self):
        original = bank_fingerprint(self.bank)
        for variant in (reordered_bank(self.bank, 17),
                        unknown_item_bank(self.bank, self.kt)):
            with self.subTest(variant=variant["questions"][0]["item_id"]):
                self.assertNotEqual(original, bank_fingerprint(variant))
                with self.assertRaisesRegex(ValueError, "approved"):
                    MCQSessionService(variant, self.store)
        self.assertEqual(original, bank_fingerprint(self.bank))

    def test_history_comparison_is_repeated_exposure_not_mastery(self):
        result = self.matrix()
        correct, incorrect = result["variants"][1:3]
        self.assertEqual(correct["history_kind"], "synthetic_repeated_approved_items")
        self.assertEqual(incorrect["history_kind"], "synthetic_repeated_approved_items")
        self.assertEqual(correct["history_length"], incorrect["history_length"])
        self.assertEqual(correct["history_length"], 20)
        self.assertNotEqual(correct["kt"]["p_correct_before_each_answer"],
                            incorrect["kt"]["p_correct_before_each_answer"])
        self.assertEqual(correct["research_bank_sha256"], result["base_bank_sha256"])
        self.assertIn("not real prior sessions", " ".join(result["limitations"]))

    def test_matrix_taxonomy_report_keeps_skill_conformal_separate(self):
        taxonomy = make_taxonomy(self.bank)
        matrix = self.matrix(taxonomy=taxonomy)
        batch = {"schema": "phase3_research_matrix_batch_v1",
                 "scope": "researcher_only", "base_bank_sha256": bank_fingerprint(self.bank),
                 "run_count": 1, "results": [matrix]}
        report = compile_matrix_taxonomy(batch, self.bank, taxonomy)
        self.assertEqual(8, len(report["subtopics"]))
        self.assertEqual(40, len(report["skill_conformal"]))
        self.assertEqual({5, 10}, {r["calibrated_k"] for r in
                                    report["skill_conformal"]})
        self.assertEqual(40, sum(row["n_items"] for row in report["subtopics"]
                                 if row["checkpoint"] == "end"))
        self.assertNotIn("conformal", json.dumps(report["subtopics"]))
        tampered = copy.deepcopy(batch)
        tampered["results"][0]["baseline"]["responses"][0]["correct"] = False
        with self.assertRaisesRegex(ValueError, "answer key"):
            compile_matrix_taxonomy(tampered, self.bank, taxonomy)
        out = Path(self.tmp.name) / "taxonomy_report"
        write_matrix_taxonomy(report, out)
        self.assertTrue((out / "subtopic_observed.csv").exists())
        self.assertTrue((out / "skill_conformal.csv").exists())
        with self.assertRaises(FileExistsError):
            write_matrix_taxonomy(report, out)

    def test_new_question_text_without_frozen_embedding_is_refused(self):
        candidate = make_bank()
        candidate["questions"][0]["text"] = "Unseen new question?"
        candidate["questions"][0]["content_text"] = "Unseen new question? [OPTIONS] alpha | beta | gamma"
        with self.assertRaisesRegex(ValueError, "not KT-text-compatible"):
            run_matrix(candidate, {"profile": "all_correct", "seed": 11},
                       self.store, self.kt, Gate(10), Gate(5))

    def test_cold_id_variant_keeps_text_compatible_and_graph_private(self):
        class Graph:
            def foundational_for(self, sid, allow_weak_evidence):
                return [{"skill_id": "unapproved_related_skill"}]

        result = self.matrix(graph=Graph())
        cold = result["variants"][3]
        self.assertEqual(cold["kind"], "unknown_item_ids_same_text")
        self.assertEqual(len(cold["kt"]["coverage"]["unknown_item_ids"]), 40)
        self.assertEqual(cold["kt"]["coverage"]["missing_content_indexes"], [])
        self.assertEqual(cold["kt"]["coverage"]["missing_option_values"], [])
        self.assertEqual(cold["history_length"], 0)
        self.assertIn("unapproved_related_skill", json.dumps(cold["graph"]))
        self.assertNotIn("unapproved_related_skill",
                         json.dumps(result["baseline"]["observed"]["end"]["student"]))


if __name__ == "__main__":
    unittest.main()
