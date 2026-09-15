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
import bisect
import csv
import gzip
import json
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).parent.parent
DATA_V2 = ROOT / "data_v2"
OUT = Path(__file__).parent / "prepared_v2"


def parse_args() -> argparse.Namespace:
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
    p.add_argument("--max-session-gap-days", type=float, default=30.0,
                    help="If the gap between two chronologically consecutive interactions "
                         "for the same student exceeds this many days, treat it as a session "
                         "boundary: sliding windows (see chunk_student_events) are never built "
                         "across it, so a student active in e.g. 2025 and returning in 2026 "
                         "doesn't get a window whose 'recent history' is actually many months "
                         "stale. Does not affect train/val/test_warm assignment (still purely "
                         "chronological-fraction based) or delta_t/time_bin computation (which "
                         "deliberately still spans the gap, so the model can see just how large "
                         "it was via the top time bin).")
    p.add_argument("--max-windows-per-student", type=int, default=8,
                    help="Cap on how many sliding windows (post session-splitting) a single "
                         "student can contribute. Hyper-active students (up to ~10k "
                         "interactions) can otherwise generate 30+ overlapping windows and "
                         "dominate the training loss. When a student exceeds this, windows are "
                         "thinned via evenly-spaced (not just earliest-N) selection so early, "
                         "middle, and late curriculum stages are all still represented -- see "
                         "cap_windows_stratified(). Set to 0 to disable capping.")
    return p.parse_args()


def safe_float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_timestamp_epoch(ts: Optional[str]) -> Optional[float]:
    """Parse an ISO-8601 timestamp (e.g. '2025-10-27T12:13:28Z') to epoch
    seconds, used for session-gap detection and delta_t/time_bin
    computation. Returns None (rather than raising) for missing/malformed
    values so a single bad timestamp can't crash the whole run -- callers
    treat None as "unknown gap", never splitting/binning across it."""
    if not ts:
        return None
    try:
        iso = ts[:-1] + "+00:00" if ts.endswith("Z") else ts
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


NUM_TIME_BINS = 32
# 31 upper-edge thresholds (seconds) log-spaced from 60s to 180 days, giving
# 32 bins total: bin 0 is "< 60s" (continuous practice), bins 1-10 land in
# minutes-to-hours, bins 11-20 in days-to-weeks, bins 21-31 in months up to
# ">180 days" (the open-ended last bin) -- matches the KT literature's usual
# log-binned inter-attempt time feature (e.g. DKT+forgetting/SAKT variants).
_TIME_BIN_EDGES = [60.0 * ((180.0 * 86400.0 / 60.0) ** (i / 30.0)) for i in range(31)]


def time_bin_of(delta_seconds: Optional[float]) -> int:
    """Discretize a raw inter-interaction gap (seconds) into one of
    NUM_TIME_BINS log-spaced bins. None/negative deltas (missing timestamp,
    or the very first event of a sequence) map to bin 0."""
    if delta_seconds is None or delta_seconds < 0:
        return 0
    idx = bisect.bisect_right(_TIME_BIN_EDGES, delta_seconds)
    return min(idx, NUM_TIME_BINS - 1)


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


def chunk_student_events(record_events: list[dict], window_size: int, context_frac: float) -> list[list[dict]]:
    """Identical windowing scheme to prepare_sequences.py -- see that file
    for the full rationale. Called once per SESSION (see
    split_into_sessions), not once per student, so a window is never built
    across a multi-month gap in a student's history."""
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


def split_into_sessions(record_events: list[dict], epochs: list[Optional[float]],
                         max_gap_seconds: float) -> list[list[dict]]:
    """Split one student's chronologically-ordered events into sessions,
    breaking wherever the gap to the previous event exceeds
    `max_gap_seconds` (see --max-session-gap-days). `epochs[i]` must be the
    parsed epoch-seconds timestamp of `record_events[i]` (None if
    unparseable -- unknown gaps never trigger a split). This only affects
    how chunk_student_events later builds sliding windows (each session is
    windowed independently, so no window spans e.g. a 2025-to-2026 gap);
    it does NOT affect train/val/test_warm assignment, which stays purely
    a function of chronological position within the student's full history."""
    if not record_events:
        return []
    sessions: list[list[dict]] = []
    current: list[dict] = [record_events[0]]
    for i in range(1, len(record_events)):
        prev_t, cur_t = epochs[i - 1], epochs[i]
        if prev_t is not None and cur_t is not None and (cur_t - prev_t) > max_gap_seconds:
            sessions.append(current)
            current = []
        current.append(record_events[i])
    sessions.append(current)
    return sessions


def cap_windows_stratified(chunks: list[list[dict]], max_windows: int) -> list[list[dict]]:
    """Thin an over-long list of chronologically-ordered sliding windows
    (post session-splitting) down to at most `max_windows`, so a single
    hyper-active student (up to ~10k interactions, 30+ windows) can't
    dominate the training loss. Selection is evenly spaced across the
    window list rather than "keep the first N", so early/middle/late
    curriculum stages are all still represented -- this also always keeps
    the first window (earliest history) and the last window (most recent
    history, where test_warm/test_cold_item positions concentrate)."""
    n = len(chunks)
    if max_windows <= 0 or n <= max_windows:
        return chunks
    if max_windows == 1:
        keep_idx = {n // 2}
    else:
        keep_idx = {round(i * (n - 1) / (max_windows - 1)) for i in range(max_windows)}
    return [chunks[i] for i in sorted(keep_idx)]


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
                "t_epoch": parse_timestamp_epoch(row["timestamp"]),
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
    students_session_split, students_capped, windows_dropped = 0, 0, 0
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
            epochs = []
            prev_epoch = None
            for i, ev in enumerate(events):
                if i < n_train:
                    split = "train"
                elif i < n_train + n_val:
                    split = "val"
                else:
                    split = "test_warm"
                if ev["item"] in cold_items:
                    split = "test_cold_item" if split != "train" else "skip_cold_in_train"
                # delta_t/time_bin deliberately span session gaps (unlike the
                # windowing below): a huge gap is meaningful signal (it lands
                # in the top time bin), not something to hide from the model.
                cur_epoch = ev.get("t_epoch")
                delta_t = 0.0 if prev_epoch is None or cur_epoch is None else max(0.0, cur_epoch - prev_epoch)
                prev_epoch = cur_epoch if cur_epoch is not None else prev_epoch
                epochs.append(cur_epoch)
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
                    "delta_t": delta_t,
                    "time_bin": time_bin_of(delta_t),
                    "split": split,
                })

            sessions = split_into_sessions(record_events, epochs, args.max_session_gap_days * 86400.0)
            if len(sessions) > 1:
                students_session_split += 1
            raw_chunks = []
            for session in sessions:
                raw_chunks.extend(chunk_student_events(session, args.max_seq_len, args.context_overlap_frac))
            if len(raw_chunks) > 1:
                students_chunked += 1
            if args.max_windows_per_student > 0 and len(raw_chunks) > args.max_windows_per_student:
                students_capped += 1
                windows_dropped += len(raw_chunks) - args.max_windows_per_student
            chunks = cap_windows_stratified(raw_chunks, args.max_windows_per_student)
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
        "max_session_gap_days": args.max_session_gap_days,
        "max_windows_per_student": args.max_windows_per_student,
        "num_time_bins": NUM_TIME_BINS,
        "students_needing_multiple_windows": students_chunked,
        "students_with_session_split": students_session_split,
        "students_with_windows_capped": students_capped,
        "windows_dropped_by_cap": windows_dropped,
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
            "Each event also carries delta_t (seconds since the student's previous interaction, "
            "0.0 for the very first) and time_bin (that delta_t log-binned into NUM_TIME_BINS=32 "
            "buckets, see time_bin_of) for the time-embedding-aware model. Sliding windows "
            "(chunk_student_events) are now built per SESSION, not per student -- a session breaks "
            "wherever the gap to the previous interaction exceeds --max-session-gap-days (default "
            "30), so no window's 'recent history' silently spans e.g. a 2025-to-2026 return gap. "
            "delta_t/time_bin themselves still span session gaps deliberately (a huge gap is real "
            "signal, not something to hide). Students whose sessions produce more windows than "
            "--max-windows-per-student (default 8) have their windows thinned via evenly-spaced "
            "selection (cap_windows_stratified) so hyper-active students (up to ~10k interactions) "
            "can't dominate the training loss; this does drop some train-only interactions "
            "entirely for such students (see students_with_windows_capped/windows_dropped_by_cap).",
        ],
    }
    (out_dir / "split_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
