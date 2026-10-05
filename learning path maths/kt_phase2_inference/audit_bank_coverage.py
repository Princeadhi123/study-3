"""Read-only availability audit, NOT a KT or conformal performance evaluation.

Counts stored non-context target occurrences, separately for underlying items
and exact bank renderings. Student identifiers remain internal and are never
written. Overlap context is counted separately, never as another target.
"""
import argparse
import csv
import gzip
import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path

import paths

SPLITS = ("train", "val", "test_warm", "test_cold_item",
          "skip_cold_in_train", "context")
HELDOUT = ("val", "test_warm", "test_cold_item")
TEST = ("test_warm", "test_cold_item")


def base_student(raw):
    base, marker, window = raw.rpartition("#w")
    return base if marker and window.isdecimal() else raw


def item_id(event):
    instance = event["item_instance"]
    template, marker, digest = instance.rpartition("__q")
    if not marker or not template or not digest:
        raise ValueError("Cannot recover source item ID from item_instance")
    return template


def histogram_summary(hist):
    n = sum(hist.values())
    if not n:
        return {"min": None, "median": None, "max": None}
    ordered = sorted(hist)
    middle = []
    for rank in ((n - 1) // 2, n // 2):
        cumulative = 0
        for value in ordered:
            cumulative += hist[value]
            if cumulative > rank:
                middle.append(value)
                break
    return {"min": ordered[0], "median": sum(middle) / 2, "max": ordered[-1]}


class Counts:
    def __init__(self):
        self.n = Counter()
        self.correct = Counter()
        self.students = defaultdict(set)
        self.with_history_students = defaultdict(set)
        self.history = defaultdict(Counter)
        self.same_skill = defaultdict(Counter)

    def add(self, event, student, position, same_skill):
        split = event["split"]
        self.n[split] += 1
        self.correct[split] += event["correct"]
        self.students[split].add(student)
        self.history[split][position] += 1
        self.same_skill[split][same_skill] += 1
        if position:
            self.with_history_students[split].add(student)

    def group(self, splits):
        history = Counter()
        same_skill = Counter()
        for split in splits:
            history.update(self.history[split])
            same_skill.update(self.same_skill[split])
        n = sum(self.n[s] for s in splits)
        correct = sum(self.correct[s] for s in splits)
        return {
            "n": n, "correct": correct, "incorrect": n - correct,
            "students": len(set().union(*(self.students[s] for s in splits))),
            "with_history_n": n - history[0],
            "with_history_students": len(set().union(
                *(self.with_history_students[s] for s in splits))),
            "zero_history_n": history[0],
            "with_same_skill_history_n": n - same_skill[0],
            "preceding_events_in_window": histogram_summary(history),
            "preceding_same_skill_events_in_window": histogram_summary(same_skill),
        }

    def export(self):
        return {
            "by_split": {s: self.group((s,)) for s in SPLITS},
            "heldout": self.group(HELDOUT),
            "test": self.group(TEST),
        }


def audit(bank, vocab, sequences, split_report):
    questions = bank["questions"]
    if not questions:
        raise ValueError("Empty question bank")
    by_rendering = {}
    item_skills = {}
    for q in questions:
        key = (q["item_id"], q["skill_id"], q["content_text"])
        if key in by_rendering:
            raise ValueError("Duplicate exact rendering in bank")
        if q["skill_id"] not in vocab["skill_vocab"]:
            raise ValueError("Bank skill missing from vocabulary")
        if q["item_id"] in item_skills and item_skills[q["item_id"]] != q["skill_id"]:
            raise ValueError("Bank item has inconsistent skills")
        by_rendering[key] = q["question_id"]
        item_skills[q["item_id"]] = q["skill_id"]
    if len({q["question_id"] for q in questions}) != len(questions):
        raise ValueError("Duplicate question ID")
    item_counts = {item: Counts() for item in item_skills}
    question_counts = {q["question_id"]: Counts() for q in questions}
    other_skill_counts = {item: Counter() for item in item_skills}
    all_exact = Counts()
    all_items = Counts()
    split_counts = Counter()
    records = 0
    max_length = split_report["max_seq_len_window"]
    with gzip.open(sequences, "rt", encoding="utf-8") as stream:
        for line in stream:
            record = json.loads(line)
            records += 1
            events = record["events"]
            if len(events) > max_length:
                raise ValueError("Record exceeds prepared window size")
            student = base_student(record["student_id"])
            prior_skills = Counter()
            for position, event in enumerate(events):
                split = event["split"]
                if split not in SPLITS or event["correct"] not in (0, 1):
                    raise ValueError("Unknown split or non-binary correctness")
                split_counts[split] += 1
                source_item = item_id(event)
                if source_item in item_counts:
                    skill = item_skills[source_item]
                    expected_item = vocab["item_vocab"].get(source_item, 1)
                    if event["item"] != expected_item:
                        raise ValueError("Matched item disagrees with vocabulary")
                    if (split in ("skip_cold_in_train", "test_cold_item")) != (
                            expected_item == 1) and split != "context":
                        raise ValueError("Matched item's split disagrees with cold-item mapping")
                    if event["skill"] != vocab["skill_vocab"][skill]:
                        other_skill_counts[source_item][split] += 1
                        prior_skills[event["skill"]] += 1
                        continue
                    same_skill = prior_skills[event["skill"]]
                    item_counts[source_item].add(event, student, position, same_skill)
                    all_items.add(event, student, position, same_skill)
                    key = (source_item, skill, event["content_text"])
                    if key in by_rendering:
                        question_counts[by_rendering[key]].add(
                            event, student, position, same_skill)
                        all_exact.add(event, student, position, same_skill)
                prior_skills[event["skill"]] += 1
            if records % 10000 == 0:
                print(f"Scanned {records:,} windows", flush=True)
    if records != split_report["sequence_records_written"]:
        raise ValueError("Sequence record count differs from split report")
    if dict(split_counts) != split_report["split_event_counts"]:
        raise ValueError("Sequence split counts differ from split report")
    return {
        "protocol": "bank_historical_availability_audit_v1",
        "scope": {
            "match": "Exact: source item + skill + byte-exact content_text including ordered options; item-level: source item + skill.",
            "count_unit": "Stored labelled target occurrence; context copies excluded from heldout/test totals. Not unique sessions or first attempts.",
            "history": "Preceding events in the retained window, including context and skipped cold labels. Not full lifetime history; position zero is not necessarily a new student.",
            "heldout": list(HELDOUT), "test": list(TEST),
            "limitations": [
                "Prepared windows are session-split and capped; this is not an exhaustive raw-log audit.",
                "val participated in checkpoint selection; cold-item results also informed joint checkpoint selection.",
                "test students must be separated from conformal calibration students before any coverage evaluation.",
                "Cold labels can occur in previous-step history despite being excluded as training targets.",
                "Exact content may have other source item IDs; this audit does not certify content-level training exclusion.",
                "No KT predictions, conformal coverage, accuracy, or fixed-assessment validity are measured.",
                "Repeated attempts are retained; no immutable source event ID is available for further deduplication.",
                "An item can occur under other skill labels; those rows are reported separately and excluded from bank-skill coverage.",
            ],
        },
        "scan": {"records": records, "split_event_counts": dict(split_counts)},
        "bank": {"questions": len(questions), "unique_items": len(item_counts)},
        "totals": {"exact_rendering": all_exact.export(), "item_level": all_items.export()},
        "items": [
            {"item_id": item, "skill_id": item_skills[item],
             "in_item_vocab": item in vocab["item_vocab"],
             "other_skill_occurrences_by_split": {
                 s: other_skill_counts[item][s] for s in SPLITS},
             **counts.export()}
            for item, counts in sorted(item_counts.items())
        ],
        "questions": [
            {"question_id": q["question_id"], "item_id": q["item_id"],
             "skill_id": q["skill_id"], **question_counts[q["question_id"]].export()}
            for q in questions
        ],
    }


def provenance(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"name": path.name, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bank", type=Path, default=paths.ARTIFACTS /
                        "test_question_bank_text_only_approved_v2.json")
    parser.add_argument("--sequences", type=Path, default=paths.SEQUENCES)
    parser.add_argument("--vocab", type=Path, default=paths.VOCAB)
    parser.add_argument("--split-report", type=Path, default=paths.SPLIT_REPORT)
    parser.add_argument("--out-dir", type=Path, default=paths.ARTIFACTS / "bank_coverage_audit")
    args = parser.parse_args()
    if args.out_dir.exists():
        raise FileExistsError("Refusing to overwrite an existing audit directory")
    inputs = {k: paths.require(getattr(args, k)) for k in
              ("bank", "sequences", "vocab", "split_report")}
    loaded = {k: json.loads(p.read_text(encoding="utf-8")) for k, p in inputs.items()
              if k != "sequences"}
    report = audit(loaded["bank"], loaded["vocab"], inputs["sequences"],
                   loaded["split_report"])
    report["inputs"] = {k: provenance(p) for k, p in inputs.items()}
    args.out_dir.mkdir(parents=True, exist_ok=False)
    with (args.out_dir / "coverage.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, indent=2, ensure_ascii=True)
        stream.write("\n")
    fields = ("question_id", "item_id", "skill_id", "split", "n", "students",
              "correct", "incorrect", "with_history_n", "with_history_students",
              "zero_history_n", "with_same_skill_history_n")
    with (args.out_dir / "coverage.csv").open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for q in report["questions"]:
            for split, counts in q["by_split"].items():
                writer.writerow({**{k: q[k] for k in fields[:3]}, "split": split,
                                 **{k: counts[k] for k in fields[4:]}})
    for level, counts in report["totals"].items():
        print(f"{level}: heldout={counts['heldout']['n']:,}; "
              f"test with history={counts['test']['with_history_n']:,}; "
              f"test students={counts['test']['students']:,}")
    print(f"Wrote aggregate-only audit to {args.out_dir}")


if __name__ == "__main__":
    main()
