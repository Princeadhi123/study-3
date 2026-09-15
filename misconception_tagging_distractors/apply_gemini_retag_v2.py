"""Fold Gemini's answers for make_gemini_retag_batch_v2.py's CSV (the ~150
rows still blank in distractor_catalog_final_v3.csv after merging run 2 --
141 explicit elicit UNKNOWN + 9 elicit call failures) into
out/distractor_catalog_final_v4.csv.

Same convention as apply_gemini_retag.py: every distinct
gemini_misconception_label (case-insensitive, trimmed) gets one fresh
MC_GEMINI_#### id, UNLESS that exact text already exists somewhere in the
catalog (e.g. Gemini happened to phrase it identically to an existing
cluster's label) -- in which case it reuses that existing id instead of
minting a redundant one, then re-canonicalizes to guarantee "same text ->
same id" continues to hold across the whole file (see
merge_run2_into_final.py's canonicalize_misconception_ids).

Expects out/gemini_retag_batch_v2.csv with its "gemini_misconception_label"
column filled in (rows left blank are skipped).

Usage:
  python apply_gemini_retag_v2.py
"""
import argparse
from pathlib import Path

import pandas as pd

from tagging_common import label_leaks_instance_value

ROOT = Path(__file__).parent


def load_correct_value_by_item(item_context_path: str) -> dict:
    if not Path(item_context_path).exists():
        print(f"Warning: no item-context file at {item_context_path}, "
              f"cannot check batch labels for leaked instance-specific values")
        return {}
    ctx = pd.read_csv(item_context_path, encoding="utf-8-sig", dtype=str, keep_default_na=False)
    return dict(zip(ctx["item_id"], ctx["correct_option_value"]))


def build_label_norm_to_id(df: pd.DataFrame) -> dict:
    """Reuse an existing id if this exact text already labels some other row
    (case-insensitive, trimmed) -- keeps "same text -> same id" true without
    waiting for a later canonicalization pass."""
    label_norm_to_id = {}
    for lbl, mid in zip(df["final_misconception_label"], df["misconception_id"]):
        norm = lbl.strip().lower()
        if norm and norm not in label_norm_to_id:
            label_norm_to_id[norm] = mid
    return label_norm_to_id


def make_id_allocator(existing_ids: set):
    """Returns a callable that mints the next unused MC_GEMINI_#### id,
    registering it in `existing_ids` so subsequent calls never repeat it."""
    state = {"n": 0}

    def next_id() -> str:
        while f"MC_GEMINI_{state['n']:04d}" in existing_ids:
            state["n"] += 1
        new_id = f"MC_GEMINI_{state['n']:04d}"
        existing_ids.add(new_id)
        state["n"] += 1
        return new_id

    return next_id


def resolve_label_id(label: str, label_norm_to_id: dict, allocate_id) -> tuple:
    """Returns (misconception_id, reused: bool) for `label`, minting a fresh
    id via `allocate_id()` only if this exact text hasn't been seen before."""
    norm = label.strip().lower()
    if norm in label_norm_to_id:
        return label_norm_to_id[norm], True
    new_id = allocate_id()
    label_norm_to_id[norm] = new_id
    return new_id, False


def apply_batch(batch: pd.DataFrame, df: pd.DataFrame, correct_value_by_item: dict,
                 label_norm_to_id: dict, allocate_id) -> dict:
    n_reused = n_new = n_skipped = 0
    leak_warnings = []
    for row in batch.itertuples():
        # is_correct_option == "0" restricts the match to the wrong-answer
        # row: the catalog can have a same-text correct-answer row too (see
        # merge_into_kt.py's docstring) -- a retag must never land on that one.
        mask = ((df["item_id"] == row.item_id) & (df["option_value"] == row.option_value)
                & (df["is_correct_option"] == "0"))
        if not mask.any():
            n_skipped += 1
            continue
        label = row.gemini_misconception_label
        leaks = label_leaks_instance_value(label, row.option_value, correct_value_by_item.get(row.item_id, ""))
        if leaks:
            leak_warnings.append((row.item_id, row.option_value, label, leaks))
        new_id, reused = resolve_label_id(label, label_norm_to_id, allocate_id)
        n_reused += int(reused)
        n_new += int(not reused)
        df.loc[mask, "misconception_id"] = new_id
        df.loc[mask, "misconception_label"] = label
        df.loc[mask, "final_misconception_label"] = label
        df.loc[mask, "label_source"] = "gemini_new_tag_run2"
        df.loc[mask, "tagging_run"] = "run2_gemini_patch"
    return {"n_reused": n_reused, "n_new": n_new, "n_skipped": n_skipped, "leak_warnings": leak_warnings}


def print_leak_warnings(leak_warnings: list) -> None:
    if not leak_warnings:
        return
    print(f"\nWARNING: {len(leak_warnings)} Gemini label(s) quote back this row's own answer "
          f"value(s) -- likely overfit to one instance instead of stating a general error "
          f"pattern (see tagging_common.label_leaks_instance_value). Review before trusting:")
    for item_id, option_value, label, leaks in leak_warnings:
        print(f"  [{item_id}] option_value={option_value!r} leaked={leaks!r}\n"
              f"      label: {label}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default=str(ROOT / "out" / "gemini_retag_batch_v2.csv"))
    parser.add_argument("--final-v3", default=str(ROOT / "out" / "distractor_catalog_final_v3.csv"))
    parser.add_argument("--item-context",
                         default=str(ROOT.parent / "learning path maths" / "kt_phase1" / "reports_v2" / "item_context.csv"))
    parser.add_argument("--out", default=str(ROOT / "out" / "distractor_catalog_final_v4.csv"))
    args = parser.parse_args()

    batch = pd.read_csv(args.batch, encoding="utf-8-sig", low_memory=False, dtype=str, keep_default_na=False)
    batch["gemini_misconception_label"] = batch["gemini_misconception_label"].fillna("").str.strip()
    batch = batch[batch["gemini_misconception_label"] != ""]
    if batch.empty:
        raise SystemExit(f"No rows in {args.batch} have a gemini_misconception_label filled in -- nothing to do.")

    df = pd.read_csv(args.final_v3, encoding="utf-8-sig", dtype=str, keep_default_na=False, low_memory=False)
    correct_value_by_item = load_correct_value_by_item(args.item_context)

    label_norm_to_id = build_label_norm_to_id(df)
    existing_ids = set(df["misconception_id"].dropna().astype(str))
    allocate_id = make_id_allocator(existing_ids)

    result = apply_batch(batch, df, correct_value_by_item, label_norm_to_id, allocate_id)

    df.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"Applied {result['n_reused'] + result['n_new']} rows ({result['n_reused']} reused an existing id, "
          f"{result['n_new']} minted a new MC_GEMINI_#### id; {result['n_skipped']} rows in batch not found "
          f"in {args.final_v3}, skipped)")
    print(f"Wrote {len(df):,} rows to {args.out}")
    print(f"Tagged rows now: {(df['final_misconception_label'].str.strip() != '').sum():,}")
    print_leak_warnings(result["leak_warnings"])


if __name__ == "__main__":
    main()
