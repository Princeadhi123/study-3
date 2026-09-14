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

ROOT = Path(__file__).parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default=str(ROOT / "out" / "gemini_retag_batch_v2.csv"))
    parser.add_argument("--final-v3", default=str(ROOT / "out" / "distractor_catalog_final_v3.csv"))
    parser.add_argument("--out", default=str(ROOT / "out" / "distractor_catalog_final_v4.csv"))
    args = parser.parse_args()

    batch = pd.read_csv(args.batch, encoding="utf-8-sig", low_memory=False, dtype=str, keep_default_na=False)
    batch["gemini_misconception_label"] = batch["gemini_misconception_label"].fillna("").str.strip()
    batch = batch[batch["gemini_misconception_label"] != ""]
    if batch.empty:
        raise SystemExit(f"No rows in {args.batch} have a gemini_misconception_label filled in -- nothing to do.")

    df = pd.read_csv(args.final_v3, encoding="utf-8-sig", dtype=str, keep_default_na=False, low_memory=False)

    # Reuse an existing id if this exact text already labels some other row
    # (case-insensitive, trimmed) -- keeps "same text -> same id" true
    # without waiting for a later canonicalization pass.
    label_norm_to_id = {}
    for lbl, mid in zip(df["final_misconception_label"], df["misconception_id"]):
        norm = lbl.strip().lower()
        if norm and norm not in label_norm_to_id:
            label_norm_to_id[norm] = mid

    existing_ids = set(df["misconception_id"].dropna().astype(str))
    n = 0
    while f"MC_GEMINI_{n:04d}" in existing_ids:
        n += 1

    n_reused = n_new = n_skipped = 0
    for row in batch.itertuples():
        mask = (df["item_id"] == row.item_id) & (df["option_value"] == row.option_value)
        if not mask.any():
            n_skipped += 1
            continue
        label = row.gemini_misconception_label
        norm = label.strip().lower()
        if norm in label_norm_to_id:
            new_id = label_norm_to_id[norm]
            n_reused += 1
        else:
            while f"MC_GEMINI_{n:04d}" in existing_ids:
                n += 1
            new_id = f"MC_GEMINI_{n:04d}"
            existing_ids.add(new_id)
            label_norm_to_id[norm] = new_id
            n += 1
            n_new += 1
        df.loc[mask, "misconception_id"] = new_id
        df.loc[mask, "misconception_label"] = label
        df.loc[mask, "final_misconception_label"] = label
        df.loc[mask, "label_source"] = "gemini_new_tag_run2"
        df.loc[mask, "tagging_run"] = "run2_gemini_patch"

    df.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"Applied {n_reused + n_new} rows ({n_reused} reused an existing id, {n_new} minted a new "
          f"MC_GEMINI_#### id; {n_skipped} rows in batch not found in {args.final_v3}, skipped)")
    print(f"Wrote {len(df):,} rows to {args.out}")
    print(f"Tagged rows now: {(df['final_misconception_label'].str.strip() != '').sum():,}")


if __name__ == "__main__":
    main()
