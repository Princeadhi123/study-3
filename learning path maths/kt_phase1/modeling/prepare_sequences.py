"""Build per-student chronological sequences for the three KT model variants.

Input:  kt_phase1/data/kt_interactions.csv.gz (item-level, already sorted by
        student then timestamp -- see build_item_level_kt.py / sort_phase1_kt.py)

Output (kt_phase1/modeling/prepared/):
  vocab.json                 skill_id / item_id -> integer index maps
  sequences.jsonl.gz         one JSON object per student: full chronological
                              interaction sequence with integer-coded fields
  split_report.json          counts for every split

Splits produced (see docstring of `assign_splits` for definitions):
  train / val / test_warm    time-ordered split within each student's own history
  test_cold_item             held-out item_ids never seen in train, evaluated
                              wherever they occur in a student's timeline

This script only prepares data (no PyTorch dependency), so it can run on a
laptop; the resulting sequences.jsonl.gz is what the actual training job
(local smoke test or supercomputer run) consumes.
"""
import argparse
import csv
import gzip
import json
import random
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA = ROOT / "data"
OUT = Path(__file__).parent / "prepared"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", default=str(DATA / "kt_interactions.csv.gz"))
    p.add_argument("--out-dir", default=str(OUT))
    p.add_argument("--min-interactions", type=int, default=10,
                   help="Drop students with fewer than this many valid interactions.")
    p.add_argument("--max-seq-len", type=int, default=400,
                   help="Truncate to the most recent N interactions per student "
                        "(applied at training time too; stored here for reference).")
    p.add_argument("--cold-item-fraction", type=float, default=0.05,
                   help="Fraction of item_ids removed from train entirely, to test "
                        "generalization to never-before-seen items.")
    p.add_argument("--val-fraction", type=float, default=0.15)
    p.add_argument("--test-fraction", type=float, default=0.15)
    p.add_argument("--limit-rows", type=int, default=None,
                   help="For quick local smoke tests: only read the first N source rows.")
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def main():
    args = parse_args()
    random.seed(args.seed)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # ---- pass 1: read everything into per-student lists (already time-sorted) ---
    students = defaultdict(list)
    skill_ids, item_ids = set(), set()
    rows_read = 0
    with gzip.open(args.source, "rt", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows_read += 1
            if args.limit_rows and rows_read > args.limit_rows:
                break
            skill_ids.add(row["skill_id"])
            item_ids.add(row["item_id"])
            students[row["student_id"]].append({
                "t": row["timestamp"],
                "skill": row["skill_id"],
                "item": row["item_id"],
                "text": row["text"],
                "item_instance": row["item_instance_id"],
                "correct": int(row["correctness"]),
                "rt": safe_float(row["response_time_ms"]),
                "attempt": safe_float(row["attempt_number"]) or 1.0,
            })
            if rows_read % 3_000_000 == 0:
                print(f"...read {rows_read:,} rows", flush=True)

    print(f"Read {rows_read:,} rows for {len(students):,} students, "
          f"{len(skill_ids):,} skills, {len(item_ids):,} items", flush=True)

    # ---- choose cold items: held out of train entirely ---
    # This MUST happen before building item_vocab (see below): cold items must
    # never get their own trained embedding row.
    all_items_sorted = sorted(item_ids)
    random.shuffle(all_items_sorted)
    n_cold = int(len(all_items_sorted) * args.cold_item_fraction)
    cold_items = set(all_items_sorted[:n_cold])
    print(f"Cold-item holdout: {len(cold_items):,} / {len(item_ids):,} items", flush=True)

    # ---- vocabularies (index 0 reserved as PAD, 1 as UNK) ---
    # Cold items are deliberately EXCLUDED from item_vocab, so they always
    # resolve to __UNK__ (index 1). If they got their own vocab slot instead,
    # that embedding row would never receive a gradient update (every
    # occurrence of a cold item is either skipped during training or scored
    # in a held-out eval split, never used as a training target) and would
    # sit at its random initialization forever -- i.e. it would inject
    # untrained noise into both the "previous interaction" input and the
    # "query" features every time a cold item appears, artificially
    # depressing test_cold_item performance for any variant that uses
    # item_id (skill_item, skill_item_content) relative to skill_only, which
    # doesn't use item_id at all. Routing cold items to UNK instead matches
    # how a genuinely novel item would be handled at inference time.
    skill_vocab = {"__PAD__": 0, "__UNK__": 1}
    for s in sorted(skill_ids):
        skill_vocab[s] = len(skill_vocab)
    item_vocab = {"__PAD__": 0, "__UNK__": 1}
    for it in sorted(item_ids - cold_items):
        item_vocab[it] = len(item_vocab)

    # ---- filter students by minimum length, then assign time-based splits ---
    kept, dropped_short = 0, 0
    split_counts = defaultdict(int)
    seq_path = out_dir / "sequences.jsonl.gz"
    with gzip.open(seq_path, "wt", encoding="utf-8") as out_f:
        for student_id, events in students.items():
            if len(events) < args.min_interactions:
                dropped_short += 1
                continue
            kept += 1
            n = len(events)
            n_val = max(1, int(n * args.val_fraction))
            n_test = max(1, int(n * args.test_fraction))
            n_train = n - n_val - n_test
            if n_train < 1:
                n_train = 1
                n_val = max(0, (n - n_train) // 2)
                n_test = n - n_train - n_val

            record_events = []
            for i, ev in enumerate(events):
                if i < n_train:
                    split = "train"
                elif i < n_train + n_val:
                    split = "val"
                else:
                    split = "test_warm"
                # A held-out cold item occurring anywhere is reassigned to its own
                # eval-only split and excluded from train regardless of position,
                # so the model never trains on it.
                if ev["item"] in cold_items:
                    split = "test_cold_item" if split != "train" else "skip_cold_in_train"
                split_counts[split] += 1
                record_events.append({
                    "skill": skill_vocab.get(ev["skill"], 1),
                    "item": item_vocab.get(ev["item"], 1),
                    "item_instance": ev["item_instance"],
                    "text": ev["text"],
                    "correct": ev["correct"],
                    "rt": ev["rt"],
                    "attempt": ev["attempt"],
                    "t": ev["t"],
                    "split": split,
                })
            out_f.write(json.dumps({"student_id": student_id, "events": record_events}) + "\n")

    (out_dir / "vocab.json").write_text(
        json.dumps({"skill_vocab": skill_vocab, "item_vocab": item_vocab}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    report = {
        "source_rows_read": rows_read,
        "students_total": len(students),
        "students_kept": kept,
        "students_dropped_too_short": dropped_short,
        "min_interactions": args.min_interactions,
        "max_seq_len_reference": args.max_seq_len,
        "n_skills": len(skill_vocab) - 2,
        "n_items_trainable": len(item_vocab) - 2,
        "n_cold_items": len(cold_items),
        "n_items_total": len(item_ids),
        "split_event_counts": dict(split_counts),
        "notes": [
            "'skip_cold_in_train' events are interactions on a cold-holdout item that would "
            "otherwise land in the train range; they are excluded from training entirely "
            "(not used as train, not used as eval) so the model truly never sees that item.",
            "'test_cold_item' events are interactions on a held-out item that occurred in the "
            "val/test time range for that student -- used to measure generalization to unseen items.",
            "'test_warm' events are the model's normal held-out future interactions for known items/skills.",
            "Cold items are NOT in item_vocab (n_items_trainable excludes them) -- they always "
            "resolve to __UNK__ (index 1) wherever they occur, so their item embedding is never "
            "an untrained/random row polluting the interaction or query features.",
        ],
    }
    (out_dir / "split_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
