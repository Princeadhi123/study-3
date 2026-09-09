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
                   help="Window size: students with more than this many interactions are "
                        "split into multiple overlapping chronological windows (see "
                        "chunk_student_events) rather than truncated, so no interactions "
                        "are discarded. Must match --max-seq-len passed to train.py.")
    p.add_argument("--context-overlap-frac", type=float, default=0.25,
                   help="Fraction of --max-seq-len used as pure attention context "
                        "(overlap) between consecutive windows for students who need "
                        "more than one window. Higher = more real history available to "
                        "each window's predictions, at the cost of more (redundant, "
                        "non-loss-contributing) compute per epoch.")
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


def chunk_student_events(record_events, window_size, context_frac):
    """Split one student's full chronological event list into one or more
    overlapping windows of at most `window_size` events each.

    Why: the model architecture (fixed causal self-attention window) and
    training-time batching need a bounded sequence length, but simply
    keeping each student's *last* `window_size` events and discarding the
    rest throws away the vast majority of the data for students with long
    histories -- on the full dataset, 34% of students have more than 400
    interactions, and truncating to 400 discards 51% of ALL interactions
    (10.5M of 13.1M rows), overwhelmingly from the early portion of long
    students' histories, which is exactly the `train` range for most of
    them. Chunking instead of truncating means every interaction is used.

    Each window after the first overlaps the previous one by
    `window_size * context_frac` events. Events in the overlapping region
    are marked with split="context": they are still fed through the model
    (so later positions in the window have real prior history to attend to,
    instead of a fresh, wrong "this is the start of the sequence" signal)
    but are excluded from training loss and eval metrics in every window
    except the one where they were originally the "new" event -- so nothing
    is double-counted.
    """
    n = len(record_events)
    if n <= window_size:
        return [record_events]

    context_size = min(max(1, int(window_size * context_frac)), window_size - 1)
    stride = window_size - context_size

    chunks = []
    covered_upto = 0
    start = 0
    while True:
        end = min(start + window_size, n)
        chunk = []
        for j in range(start, end):
            ev = dict(record_events[j])
            if j < covered_upto:
                ev["split"] = "context"
            chunk.append(ev)
        chunks.append(chunk)
        covered_upto = max(covered_upto, end)
        if end >= n:
            break
        start += stride
        if n - start < window_size:
            # Snap the final window so it ends exactly at n (full-length
            # where possible) instead of leaving a small dangling window.
            start = max(0, n - window_size)
    return chunks


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
    students_chunked, chunks_written = 0, 0
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

            chunks = chunk_student_events(record_events, args.max_seq_len, args.context_overlap_frac)
            if len(chunks) > 1:
                students_chunked += 1
            chunks_written += len(chunks)
            for w, chunk in enumerate(chunks):
                for ev in chunk:
                    split_counts[ev["split"]] += 1
                out_id = student_id if len(chunks) == 1 else f"{student_id}#w{w}"
                out_f.write(json.dumps({"student_id": out_id, "events": chunk}) + "\n")

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
        "max_seq_len_window": args.max_seq_len,
        "context_overlap_frac": args.context_overlap_frac,
        "students_needing_multiple_windows": students_chunked,
        "sequence_records_written": chunks_written,
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
            "Students with more than max_seq_len_window interactions are split into multiple "
            "overlapping windows (one 'sequence_records_written' row each) instead of being "
            "truncated to their most recent max_seq_len_window events, so no interactions are "
            "discarded. 'context' events are the overlapping lead-in of a later window: they "
            "give the model real prior history to attend to but are excluded from training "
            "loss and eval metrics (their original split already counted them in an earlier "
            "window), so nothing is double-counted.",
        ],
    }
    (out_dir / "split_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
