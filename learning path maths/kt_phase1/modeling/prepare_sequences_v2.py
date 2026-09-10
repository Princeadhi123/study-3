"""v2 of prepare_sequences.py: reads the option-aware item-level file
(kt_phase1/data_v2/kt_interactions_v2_item_level.csv.gz, produced by
build_v2_raw_clean.py -> build_v2_sort.py -> build_v2_item_level.py) instead
of the v1 file. Two things are added on top of what prepare_sequences.py
writes:

1. `content_text` per event -- the CURRENT question's full safe content
   representation: question text, plus (when the exercise type offers
   discrete options) the option VALUES joined together, e.g.
   "(- 2) . (- 2) . (- 2) . (- 2) [OPTIONS] (-2)4 | -4.2 | -8 | -24". This is
   what gets embedded for the "content" variants (skill_item_content and
   skill_item_content_option) via embed_questions.py -- see
   build_content_text() below for exactly what is/isn't included and why.
   `text` (question only, no options) is also still kept for reference/back-
   compat with v1 embeddings.
2. `selected_text` per event -- the actual text of the option/distractor the
   student picked (e.g. "-24", not "option index 3"), used ONLY as
   PREVIOUS-step history (never for the current step being predicted --
   that would leak the label) by the skill_item_content_option variant. It
   is embedded in the SAME multilingual embedding space as content_text (by
   embed_questions.py), so the model sees what was actually selected, not
   an arbitrary position -- a bare option-index embedding can't tell that
   "option 1" in one question ("4") and "option 1" in another ("20") mean
   completely different things, this fixes that. Value is
   "[NO_OPTIONS]" (a real sentinel string, itself embedded like any other
   text) when has_options == False for that row.
   `option_idx` (the raw position, -2/-1/0..N-1) is also still kept per
   event for reporting/analysis, but the model itself uses selected_text.

Everything else (windowing, splits, cold-item holdout, vocab construction)
is identical to prepare_sequences.py -- see that file's docstring/comments
for the full rationale, not repeated here.

Rows with correctness_available=0 (~4.9% of the raw data -- exercise types
that never report Correctness, e.g. MATCH_PAIRS/CLASSIFICATION/MATSEL) are
dropped here, same as v1 implicitly dropped them: a KT correctness-prediction
target needs a known label. They are NOT dropped from
data_v2/kt_interactions_v2_item_level.csv.gz itself -- that file keeps every
row with the full raw question/options/answer for future use (e.g. once a
type-specific way to derive correctness for those types exists).
"""
import argparse
import csv
import gzip
import json
import random
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent.parent
DATA_V2 = ROOT / "data_v2"
OUT = Path(__file__).parent / "prepared_v2"


def parse_args():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", default=str(DATA_V2 / "kt_interactions_v2_item_level.csv.gz"))
    p.add_argument("--out-dir", default=str(OUT))
    p.add_argument("--min-interactions", type=int, default=10)
    p.add_argument("--max-seq-len", type=int, default=400)
    p.add_argument("--context-overlap-frac", type=float, default=0.25)
    p.add_argument("--cold-item-fraction", type=float, default=0.05)
    p.add_argument("--val-fraction", type=float, default=0.15)
    p.add_argument("--test-fraction", type=float, default=0.15)
    p.add_argument("--limit-rows", type=int, default=None)
    p.add_argument("--seed", type=int, default=42)
    return p.parse_args()


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


NO_OPTIONS_TOKEN = "[NO_OPTIONS]"


def option_idx_of(row):
    """Raw option POSITION (-2/-1/0..N-1) -- kept for reporting/analysis
    only. The model itself uses selected_text_of(), not this, so that
    "option 1" isn't treated as one fixed meaning across different
    questions (see module docstring)."""
    if row["has_options"] not in ("True", "1", "true"):
        return -2
    raw = row["selected_option_index"]
    if raw in ("", "-1"):
        return -1
    try:
        return int(float(raw))
    except ValueError:
        return -1


def selected_text_of(row):
    """The actual text of what the student selected, for use as PREVIOUS-
    step history by skill_item_content_option (see model.py's use_option).
    build_v2_raw_clean.py always populates selected_option_value with the
    raw answer text whenever has_options is True -- even when it couldn't
    be matched to a specific offered option (selected_option_index == -1)
    -- so this is meaningful text even in that case. NO_OPTIONS_TOKEN for
    rows whose exercise type has no discrete options at all."""
    if row["has_options"] not in ("True", "1", "true"):
        return NO_OPTIONS_TOKEN
    value = row.get("selected_option_value") or ""
    return value if value else NO_OPTIONS_TOKEN


def build_content_text(text, options_json_raw):
    """The CURRENT-question content representation embedded for the
    'content' variants (skill_item_content, skill_item_content_option).

    Includes the question text AND, when the exercise type offers discrete
    options (has_options=True -- mcq/single_answer_text families, see
    build_v2_raw_clean.py), the option VALUES -- but never which one is
    correct and never which one the student picked. Only information that
    exists before the student answers is safe to put in the current-step
    query; correctness/selection are supervision targets and would leak the
    label if included here (they're still used, but only as PREVIOUS-step
    history -- see option_idx_of() / model.py's use_option).

    Options are joined in their original (offered) order, not sorted, since
    the order itself is part of what the student saw.
    """
    if not options_json_raw or options_json_raw == "[]":
        return text
    try:
        options = json.loads(options_json_raw)
    except json.JSONDecodeError:
        return text
    values = [str(o.get("value", "")) for o in options if isinstance(o, dict)]
    if not values:
        return text
    return text + " [OPTIONS] " + " | ".join(values)


def chunk_student_events(record_events, window_size, context_frac):
    """Identical windowing scheme to prepare_sequences.py -- see that file
    for the full rationale."""
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
            start = max(0, n - window_size)
    return chunks


def main():
    args = parse_args()
    random.seed(args.seed)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    students = defaultdict(list)
    skill_ids, item_ids = set(), set()
    rows_read, rows_kept, rows_no_correctness = 0, 0, 0
    with gzip.open(args.source, "rt", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows_read += 1
            if args.limit_rows and rows_read > args.limit_rows:
                break
            if row["correctness_available"] not in ("1", "True", "true"):
                rows_no_correctness += 1
                continue
            rows_kept += 1
            skill_ids.add(row["skill_id"])
            item_ids.add(row["item_id"])
            students[row["student_id"]].append({
                "t": row["timestamp"],
                "skill": row["skill_id"],
                "item": row["item_id"],
                "text": row["text"],
                "content_text": build_content_text(row["text"], row["options_json"]),
                "item_instance": row["item_instance_id"],
                "correct": int(row["correctness"]),
                "rt": safe_float(row["response_time_ms"]),
                "attempt": safe_float(row["attempt_number"]) or 1.0,
                "option_idx": option_idx_of(row),
                "selected_text": selected_text_of(row),
                "option_count": int(row["option_count"] or 0),
                "exercise_family": row["exercise_family"],
            })
            if rows_read % 3_000_000 == 0:
                print(f"...read {rows_read:,} rows", flush=True)

    print(f"Read {rows_read:,} rows ({rows_kept:,} with known correctness, "
          f"{rows_no_correctness:,} dropped for missing correctness) for "
          f"{len(students):,} students, {len(skill_ids):,} skills, "
          f"{len(item_ids):,} items", flush=True)

    all_items_sorted = sorted(item_ids)
    random.shuffle(all_items_sorted)
    n_cold = int(len(all_items_sorted) * args.cold_item_fraction)
    cold_items = set(all_items_sorted[:n_cold])
    print(f"Cold-item holdout: {len(cold_items):,} / {len(item_ids):,} items", flush=True)

    skill_vocab = {"__PAD__": 0, "__UNK__": 1}
    for s in sorted(skill_ids):
        skill_vocab[s] = len(skill_vocab)
    item_vocab = {"__PAD__": 0, "__UNK__": 1}
    for it in sorted(item_ids - cold_items):
        item_vocab[it] = len(item_vocab)

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
                if ev["item"] in cold_items:
                    split = "test_cold_item" if split != "train" else "skip_cold_in_train"
                record_events.append({
                    "skill": skill_vocab.get(ev["skill"], 1),
                    "item": item_vocab.get(ev["item"], 1),
                    "item_instance": ev["item_instance"],
                    "text": ev["text"],
                    "content_text": ev["content_text"],
                    "correct": ev["correct"],
                    "rt": ev["rt"],
                    "attempt": ev["attempt"],
                    "option_idx": ev["option_idx"],
                    "selected_text": ev["selected_text"],
                    "option_count": ev["option_count"],
                    "exercise_family": ev["exercise_family"],
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
        "source_rows_dropped_missing_correctness": rows_no_correctness,
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
            "Same windowing/split/cold-item scheme as prepare_sequences.py (v1); see that file.",
            "New here: each event carries content_text (question text, plus offered option VALUES "
            "when the exercise type has them -- never correctness/selection, see build_content_text) "
            "for the skill_item_content / skill_item_content_option variants' current-step query, "
            "and selected_text (the actual text of what the student picked, or [NO_OPTIONS] if the "
            "exercise type has no discrete options, see selected_text_of) used ONLY as previous-step "
            "history by skill_item_content_option (never for the current step -- would leak the "
            "label). Both are embedded in the same multilingual space so the model sees the actual "
            "selected answer's meaning, not an arbitrary option position.",
            "Rows with correctness_available=0 are dropped before sequence-building (no label to "
            "train/eval against) -- see build_v2_raw_clean.py notes for which exercise types "
            "these are.",
        ],
    }
    (out_dir / "split_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
