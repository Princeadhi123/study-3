"""Synthetic checks for calibration exclusion and candidate availability."""
import csv
import gzip
import json
import tempfile
import unittest
from collections import defaultdict
from pathlib import Path

import numpy as np

from inventory_evaluation_candidates import (
    BlockSupport, add_window_blocks, load_all_skills, load_catalog, prompt_support, rank_skills,
    reconcile_all_skill_counts, scan, student_partition, write_table,
)

VOCAB = {"skill_vocab": {"skill-a": 2}, "item_vocab": {"warm": 2}}
SKILLS = {"skill-a": "Arithmetic"}
CONTENT = "Question? [OPTIONS] 1 | 2"
META = {"partial": False, "variant": "D", "seed": 42, "checkpoint": "/remote/model.pt",
        "n_windows": 5, "n_events": 10}


def calibration():
    return {
        "model": {"variant": "D", "seed": 42, "checkpoint": "C:/local/model.pt"},
        "calibration_protocol": {
            "split_source": "test_warm + test_cold_item, partitioned BY STUDENT",
            "calib_frac": 0.5, "seed": 123, "n_calib_students": 2, "n_eval_students": 2},
        "item_level": {"groups": {
            "warm": {"n": {"0": 0, "1": 2}}, "cold": {"n": {"0": 1, "1": 0}}}},
        "checkpoint_level": {"k": 2},
    }


def event(item="warm", split="test_warm", correct=1, skill=2, content=CONTENT):
    return {"item_instance": item + "__qhash", "item": 2 if item == "warm" else 1,
            "skill": skill, "split": split, "correct": correct,
            "content_text": content, "exercise_family": "mcq"}


def question(item):
    return {"question_id": item + "__qhash__okey", "item_id": item,
            "skill_id": "skill-a", "skill_name": "Arithmetic", "text": "Question?",
            "options": ["1", "2"], "content_text": CONTENT, "answer_index": 0,
            "in_model_catalog": True, "review_status": "unreviewed"}


class InventoryTests(unittest.TestCase):
    def fixture(self, calibration_override=None):
        raw = ["PRIVATE_A#w0", "PRIVATE_A#w1", "PRIVATE_B#w0",
               "PRIVATE_C#w0", "PRIVATE_D#w0"]
        records = [
            {"student_id": raw[0], "events": [event(split="train"), event()]},
            {"student_id": raw[1], "events": [event()]},
            {"student_id": raw[2], "events": [
                event(split="context", skill=3), event(split="val"),
                event(correct=0), event("cold-one", "test_cold_item"),
                event(content="Question? [OPTIONS] 2 | 1")]},
            {"student_id": raw[3], "events": [
                event(), event("cold-one", "skip_cold_in_train"),
                event("cold-one", "test_cold_item", correct=0),
                event("cold-two", "test_cold_item")]},
            {"student_id": raw[4], "events": [event("cold-one", "test_cold_item", correct=0)]},
        ]
        split_report = {"max_seq_len_window": 400, "sequence_records_written": 5,
                        "split_event_counts": {"train": 1, "test_warm": 5, "context": 1,
                                               "val": 1, "test_cold_item": 4,
                                               "skip_cold_in_train": 1}}
        catalog = {}
        for item in ("warm", "cold-one"):
            q = question(item)
            catalog[(item + "__qhash", 2, CONTENT)] = {
                k: q[k] for k in ("question_id", "item_id", "skill_id", "skill_name",
                                 "text", "options", "review_status")}
            catalog[(item + "__qhash", 2, CONTENT)]["review_flags"] = ["needs_review"]
        cal = calibration()
        if calibration_override:
            calibration_override(cal)
        with tempfile.TemporaryDirectory() as tmp:
            sequences = Path(tmp) / "sequences.gz"
            with gzip.open(sequences, "wt", encoding="utf-8") as stream:
                for record in records:
                    stream.write(json.dumps(record) + "\n")
            return scan(sequences, VOCAB, split_report,
                        ["PRIVATE_A", "PRIVATE_B", "PRIVATE_C", "PRIVATE_D"],
                        {"PRIVATE_A", "PRIVATE_D"}, catalog, SKILLS, cal, META, raw)

    def test_exclusion_exact_rendering_history_and_unknown_item_identity(self):
        report = self.fixture()
        warm, cold = report["warm_candidates"][0], report["cold_candidates"][0]
        self.assertEqual(report["scan"]["excluded_calibration_records"], 3)
        self.assertEqual(report["partition"]["calibration_count_check"],
                         "passed for both labels in both regimes")
        self.assertEqual((warm["n"], warm["students"], warm["correct"], warm["incorrect"]),
                         (2, 2, 1, 1))
        self.assertEqual(warm["with_history_n"], 1)
        self.assertEqual(warm["preceding_events_in_window"]["median"], 1.0)
        self.assertEqual(warm["preceding_same_skill_events_in_window"]["median"], 0.5)
        self.assertEqual(cold["n"], 2)
        self.assertEqual(cold["preceding_events_in_window"]["median"], 2.5)
        self.assertEqual(cold["preceding_same_skill_events_in_window"]["median"], 2.0)
        self.assertEqual({q["item_id"] for q in report["item_coverage"]},
                         {"warm", "cold-one", "cold-two"})
        summaries = {r["regime"]: r for r in report["summary"]}
        self.assertEqual(summaries["warm"]["all_item_answers"]["n"], 3)
        self.assertEqual(summaries["warm"]["catalog_mcq_answers"]["n"], 2)
        self.assertEqual(summaries["cold"]["all_item_answers"]["n"], 3)
        self.assertEqual(summaries["cold"]["mcq_renderings_with_both_labels"], 1)
        self.assertNotIn("PRIVATE_", json.dumps(report))
        self.assertNotIn("answer_index", json.dumps(report))
        support = report["checkpoint_support"][0]
        self.assertEqual(support["k"], 2)
        self.assertEqual(support["groups"]["mixed"]["all_test"]["n"], 2)
        self.assertEqual(support["groups"]["mixed"]["fully_catalogued"]["students"], 2)
        self.assertEqual(support["groups"]["cold_only"]["all_test"]["n"], 0)

    def test_calibration_reconstruction_must_match(self):
        with self.assertRaisesRegex(ValueError, "calibration label counts"):
            self.fixture(lambda c: c["item_level"]["groups"]["warm"]["n"].update({"1": 3}))

    def test_partition_preserves_first_seen_order_and_groups_all_windows(self):
        raw = ["PRIVATE_B#w0", "PRIVATE_A#w0", "PRIVATE_B#w1", "PRIVATE_C", "PRIVATE_D#w2"]
        students, excluded = student_partition(raw, META, calibration())
        self.assertEqual(students, ["PRIVATE_B", "PRIVATE_A", "PRIVATE_C", "PRIVATE_D"])
        perm = np.random.default_rng(123).permutation(4)
        self.assertEqual(excluded, {students[i] for i in perm[:2]})
        self.assertEqual(len(excluded), 2)

    def test_partial_wrong_model_and_wrong_partition_fail(self):
        for changes, error in (({"partial": True}, "complete"),
                               ({"seed": 43}, "model differ"),
                               ({"checkpoint": "other.pt"}, "checkpoint differs")):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, error):
                student_partition(["a", "b", "c", "d"], {**META, **changes}, calibration())
        with self.assertRaisesRegex(ValueError, "student counts"):
            student_partition(["a", "b"], META, calibration())

    def test_catalog_join_and_flag_alignment(self):
        with tempfile.TemporaryDirectory() as tmp:
            inventory, flags = Path(tmp) / "inventory.gz", Path(tmp) / "flags.gz"
            q = question("warm")
            with gzip.open(inventory, "wt", encoding="utf-8") as stream:
                stream.write(json.dumps(q) + "\n")
            with gzip.open(flags, "wt", encoding="utf-8") as stream:
                stream.write(json.dumps({"question_id": q["question_id"],
                                         "skill_id": "skill-a", "flags": ["visual"]}) + "\n")
            catalog = load_catalog(inventory, flags, SKILLS, VOCAB)
            entry = catalog[("warm__qhash", 2, CONTENT)]
            self.assertEqual(entry["review_flags"], ["visual"])
            self.assertNotIn("answer_index", entry)
            with gzip.open(flags, "wt", encoding="utf-8") as stream:
                stream.write(json.dumps({"question_id": "wrong", "skill_id": "skill-a"}) + "\n")
            with self.assertRaisesRegex(ValueError, "misaligned"):
                load_catalog(inventory, flags, SKILLS, VOCAB)

    def test_csv_has_counts_medians_flags_and_no_keys(self):
        report = self.fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "warm.csv"
            write_table(path, report["warm_candidates"], True)
            with path.open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(rows[0]["n"], "2")
            self.assertEqual(rows[0]["history_median"], "1.0")
            self.assertEqual(json.loads(rows[0]["review_flags_json"]), ["needs_review"])
            self.assertNotIn("answer_index", rows[0])

    def test_all_skills_requires_complete_vocabulary_aligned_catalog(self):
        vocab = {"skill_vocab": {"__PAD__": 0, "__UNK__": 1, "skill-a": 2, "skill-b": 3}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "skills.csv"
            path.write_text("skill_id,skill_name,vocab_idx\n"
                            "skill-b,Fractions,3\nskill-a,Arithmetic,2\n"
                            "other,Outside model,\n", encoding="utf-8")
            self.assertEqual(load_all_skills(path, vocab),
                             {"skill-a": "Arithmetic", "skill-b": "Fractions"})
            path.write_text("skill_id,skill_name,vocab_idx\nskill-a,Arithmetic,2\n",
                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "every model skill"):
                load_all_skills(path, vocab)
            path.write_text("skill_id,skill_name,vocab_idx\nskill-a,Arithmetic,3\n",
                            encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "misindexed"):
                load_all_skills(path, vocab)

    def test_blocks_preserve_grouping_regime_diversity_and_window_boundaries(self):
        blocks = defaultdict(BlockSupport)
        warm_a, warm_b = event(), event()
        warm_b["item_instance"] = "warm__qother"
        cold = event("cold-one", "test_cold_item")
        members = [(0, warm_a, True), (1, warm_b, True),
                   (2, cold, True), (3, cold, True),
                   (4, warm_a, False), (5, cold, True),
                   (6, warm_a, True)]
        add_window_blocks({"skill-a": members}, "PRIVATE", 2, blocks)
        self.assertEqual(blocks[("skill-a", "warm_only", "all_test")].export()["n"], 1)
        warm = blocks[("skill-a", "warm_only", "fully_catalogued")].export()
        self.assertEqual(warm["blocks_with_k_distinct_renderings"], 1)
        cold_counts = blocks[("skill-a", "cold_only", "fully_catalogued")].export()
        self.assertEqual(cold_counts["blocks_with_k_distinct_renderings"], 0)
        self.assertEqual(blocks[("skill-a", "mixed", "all_test")].export()["n"], 1)
        self.assertEqual(blocks[("skill-a", "mixed", "fully_catalogued")].export()["n"], 0)
        add_window_blocks({"skill-a": [(0, warm_a, True)]}, "PRIVATE", 2, blocks)
        self.assertEqual(warm, blocks[("skill-a", "warm_only", "fully_catalogued")].export())

    def test_different_skills_never_form_one_block(self):
        blocks = defaultdict(BlockSupport)
        add_window_blocks({"skill-a": [(0, event(), True)],
                           "skill-b": [(1, event(skill=3), True)]}, "PRIVATE", 2, blocks)
        self.assertEqual(len(blocks), 0)

    def test_checkpoint_reference_count_reconciliation(self):
        report = self.fixture()
        # Synthetic fixture has one unmatched-skill context row, no test row;
        # thus the selected skill covers all evaluation test targets.
        coverage = {"checkpoint_level": [{"label": "eval_warm", "n": 0},
                                         {"label": "eval_cold", "n": 2},
                                         {"label": "eval_all", "n": 2}]}
        reconcile_all_skill_counts(report, coverage)
        self.assertEqual(report["scan"]["checkpoint_count_check"]["status"], "passed")
        coverage["checkpoint_level"][1]["n"] = 3
        with self.assertRaisesRegex(ValueError, "checkpoint counts"):
            reconcile_all_skill_counts(report, coverage)

    def test_skill_ranking_prefers_two_regime_support_not_outcome_success(self):
        report = {"summary": [], "checkpoint_support": []}
        for skill, warm_repeat, cold_repeat in (("balanced", 2, 2), ("warm-rich", 100, 0)):
            for regime, repeat in (("warm", warm_repeat), ("cold", cold_repeat)):
                report["summary"].append({
                    "skill_id": skill, "regime": regime,
                    "supported_mcq_renderings": repeat,
                    "mcq_renderings_multiple_students": repeat,
                    "mcq_renderings_multiple_students_no_blocking_flags": repeat,
                    "catalog_mcq_answers": {
                        "n": repeat * 2, "students": repeat * 2,
                        "correct": repeat * 2, "incorrect": 0,
                        "preceding_events_in_window": {"median": 5}},
                })
            empty = BlockSupport().export()
            report["checkpoint_support"].append({
                "skill_id": skill, "skill_name": skill, "k": 10,
                "groups": {kind: {"all_test": empty, "fully_catalogued": empty}
                           for kind in ("warm_only", "cold_only", "mixed")},
            })
        rows = rank_skills(report)
        self.assertEqual([r["skill_id"] for r in rows], ["balanced", "warm-rich"])
        self.assertEqual(rows[0]["balanced_repeated_unblocked_renderings"], 2)
        report["summary"][0]["catalog_mcq_answers"].update(correct=0, incorrect=4)
        self.assertEqual([r["skill_id"] for r in rank_skills(report)],
                         ["balanced", "warm-rich"])

    def test_prompt_support_collapses_variants_without_pooling_singletons(self):
        def candidate(text, students=2, flags=None):
            return {"skill_id": "s", "text": text, "students": students,
                    "review_flags": flags or []}
        report = {
            "skill_ranking": [{"rank": 1, "skill_id": "s", "skill_name": "Skill"}],
            "warm_candidates": [
                candidate("Prompt A"), candidate("  PROMPT   A "),
                candidate("Prompt B", students=1), candidate("Prompt B", students=1),
                candidate("Prompt C", flags=["possible_missing_visual_or_external_context"])],
            "cold_candidates": [candidate("Prompt D"), candidate("Prompt E")],
        }
        row = prompt_support(report)[0]
        self.assertEqual(row["warm_renderings_repeated_nonblocking"], 2)
        self.assertEqual(row["warm_distinct_prompts_repeated_nonblocking"], 1)
        self.assertEqual(row["cold_distinct_prompts_repeated_nonblocking"], 2)
        self.assertEqual(row["balanced_distinct_prompts"], 1)


if __name__ == "__main__":
    unittest.main()
