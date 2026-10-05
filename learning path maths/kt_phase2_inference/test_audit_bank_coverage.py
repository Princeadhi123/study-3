"""Synthetic checks for the availability audit's counting and matching rules."""
import gzip
import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from audit_bank_coverage import audit, base_student, histogram_summary


def event(item="warm", split="train", content="A [OPTIONS] 1 | 2", correct=1):
    return {"item_instance": item + "__qhash", "item": 2 if item == "warm" else 1,
            "skill": 2, "split": split, "content_text": content, "correct": correct}


class AuditTests(unittest.TestCase):
    def run_audit(self, records, alter_report=None):
        bank = {"questions": [
            {"question_id": "qa", "item_id": "warm", "skill_id": "s",
             "content_text": "A [OPTIONS] 1 | 2"},
            {"question_id": "qb", "item_id": "warm", "skill_id": "s",
             "content_text": "B [OPTIONS] 1 | 2"},
            {"question_id": "qc", "item_id": "cold", "skill_id": "s",
             "content_text": "A [OPTIONS] 1 | 2"},
            {"question_id": "qd", "item_id": "absent", "skill_id": "s",
             "content_text": "D [OPTIONS] 1 | 2"},
        ]}
        vocab = {"skill_vocab": {"s": 2}, "item_vocab": {"warm": 2}}
        splits = Counter(e["split"] for r in records for e in r["events"])
        report = {"max_seq_len_window": 400, "sequence_records_written": len(records),
                  "split_event_counts": dict(splits)}
        if alter_report:
            alter_report(report)
        with tempfile.TemporaryDirectory() as tmp:
            sequences = Path(tmp) / "sequences.gz"
            with gzip.open(sequences, "wt", encoding="utf-8") as stream:
                for record in records:
                    stream.write(json.dumps(record) + "\n")
            return audit(bank, vocab, sequences, report)

    def test_exact_vs_template_context_history_and_student_union(self):
        records = [
            {"student_id": "PRIVATE_LEARNER#w0", "events": [
                event(), event(split="val", correct=0),
                event(split="test_warm", content="A [OPTIONS] 2 | 1")]},
            {"student_id": "PRIVATE_LEARNER#w1", "events": [
                event(split="context"), event("cold", "skip_cold_in_train"),
                event(split="test_warm"), event("cold", "test_cold_item", correct=0)]},
            {"student_id": "SECOND_PRIVATE_LEARNER", "events": [
                event(split="test_warm", content="B [OPTIONS] 1 | 2")]},
        ]
        result = self.run_audit(records)
        qa, qb, qc, qd = result["questions"]
        self.assertEqual(result["bank"], {"questions": 4, "unique_items": 3})
        self.assertEqual(qa["heldout"]["n"], 2)
        self.assertEqual(qa["heldout"]["students"], 1)
        self.assertEqual(qa["by_split"]["context"]["n"], 1)
        self.assertEqual(qa["test"]["with_history_n"], 1)
        self.assertEqual(qa["test"]["preceding_events_in_window"]["median"], 2)
        self.assertEqual(qa["test"]["preceding_same_skill_events_in_window"]["min"], 2)
        self.assertEqual(qb["test"]["zero_history_n"], 1)
        self.assertEqual(qb["test"]["with_history_students"], 0)
        self.assertEqual(qc["test"]["incorrect"], 1)
        self.assertEqual(qc["heldout"]["n"], 1)
        self.assertEqual(qd["heldout"]["n"], 0)
        self.assertIsNone(qd["heldout"]["preceding_events_in_window"]["median"])
        self.assertEqual(result["totals"]["exact_rendering"]["heldout"]["n"], 4)
        self.assertEqual(result["totals"]["item_level"]["heldout"]["n"], 5)
        self.assertEqual(result["totals"]["exact_rendering"]["test"]["students"], 2)
        self.assertNotIn("PRIVATE_LEARNER", json.dumps(result))

    def test_cold_ids_do_not_collapse_into_unknown_index(self):
        result = self.run_audit([{"student_id": "x", "events": [
            event("other_cold", "test_cold_item"),
            event("cold", "test_cold_item")]}])
        self.assertEqual(result["questions"][2]["test"]["n"], 1)
        self.assertEqual(result["questions"][2]["test"]["with_history_n"], 1)

    def test_repeated_targets_retained_not_deduplicated_by_content(self):
        result = self.run_audit([{"student_id": "x", "events": [
            event(split="test_warm"), event(split="test_warm")]}])
        self.assertEqual(result["questions"][0]["test"]["n"], 2)
        self.assertEqual(result["questions"][0]["test"]["students"], 1)

    def test_other_skill_rows_excluded_but_remain_history(self):
        other = event(split="test_warm")
        other["skill"] = 3
        result = self.run_audit([{"student_id": "x", "events": [
            other, event(split="test_warm")]}])
        qa = result["questions"][0]["test"]
        self.assertEqual(qa["n"], 1)
        self.assertEqual(qa["with_history_n"], 1)
        self.assertEqual(qa["with_same_skill_history_n"], 0)
        warm = next(i for i in result["items"] if i["item_id"] == "warm")
        self.assertEqual(warm["other_skill_occurrences_by_split"]["test_warm"], 1)

    def test_split_report_mismatch_fails(self):
        with self.assertRaisesRegex(ValueError, "record count"):
            self.run_audit([], lambda r: r.update(sequence_records_written=1))

    def test_unknown_split_fails(self):
        with self.assertRaisesRegex(ValueError, "Unknown split"):
            self.run_audit([{"student_id": "x", "events": [event(split="mystery")]}])

    def test_wrong_vocab_or_cold_split_fails(self):
        bad = event()
        bad["item"] = 7
        with self.assertRaisesRegex(ValueError, "vocabulary"):
            self.run_audit([{"student_id": "x", "events": [bad]}])
        with self.assertRaisesRegex(ValueError, "cold-item mapping"):
            self.run_audit([{"student_id": "x", "events": [event("cold", "train")]}])

    def test_student_suffix_and_histogram(self):
        self.assertEqual(base_student("x#w12"), "x")
        self.assertEqual(base_student("x#wtext"), "x#wtext")
        self.assertEqual(histogram_summary(Counter({0: 1, 3: 2, 8: 1})),
                         {"min": 0, "median": 3.0, "max": 8})


if __name__ == "__main__":
    unittest.main()
