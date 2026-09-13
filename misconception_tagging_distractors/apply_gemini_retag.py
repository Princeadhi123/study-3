"""Fold Gemini's answers for the make_gemini_retag_batch.py CSV (the 641
stage-1 UNKNOWN rows + 15 verify-stage rows whose verify call failed) into
the final tagged catalog, producing an updated
out/distractor_catalog_final_v2.csv.

Expects out/gemini_retag_batch.csv with its "gemini_misconception_label"
column filled in (rows left blank are skipped -- Gemini didn't have enough
info, or it's not been reviewed yet).

Handling per row's `task`:
  - **new_tag** rows had no misconception_id/label at all. Every distinct
    gemini_misconception_label (case-insensitive, trimmed) gets a fresh
    MC_GEMINI_#### id, so rows Gemini gave the same phrasing to land in the
    same "cluster" -- same convention as the original clustering stage.
  - **verify_failed** rows already have an id from their original
    auto-assigned cluster; only final_misconception_label/label_source
    change (id is left as-is), matching how a normal verify REPLACE is
    handled in apply_review.py -- the assignment can be corrected without
    manufacturing a new cluster id for what's still fundamentally that
    cluster's row.

Usage:
  python apply_gemini_retag.py
  python apply_gemini_retag.py --batch out/gemini_retag_batch.csv \\
      --final out/distractor_catalog_final.csv \\
      --out out/distractor_catalog_final_v2.csv
"""
import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch", default=str(ROOT / "out" / "gemini_retag_batch.csv"))
    parser.add_argument("--final", default=str(ROOT / "out" / "distractor_catalog_final.csv"))
    parser.add_argument("--out", default=str(ROOT / "out" / "distractor_catalog_final_v2.csv"))
    args = parser.parse_args()

    batch = pd.read_csv(args.batch, encoding="utf-8-sig", low_memory=False)
    batch["gemini_misconception_label"] = batch["gemini_misconception_label"].fillna("").str.strip()
    batch = batch[batch["gemini_misconception_label"] != ""]
    if batch.empty:
        raise SystemExit(f"No rows in {args.batch} have a gemini_misconception_label filled in -- nothing to do.")

    df = pd.read_csv(args.final, encoding="utf-8-sig", low_memory=False)

    # Assign new cluster ids to new_tag rows, grouping identical labels
    # together (case-insensitive) just like the original clustering stage.
    existing_ids = set(df["misconception_id"].dropna().astype(str))
    n = 0
    while f"MC_GEMINI_{n:04d}" in existing_ids:
        n += 1
    label_to_id = {}
    new_tag = batch[batch["task"] == "new_tag"]
    for label in sorted(new_tag["gemini_misconception_label"].str.lower().unique()):
        while f"MC_GEMINI_{n:04d}" in existing_ids:
            n += 1
        label_to_id[label] = f"MC_GEMINI_{n:04d}"
        n += 1

    n_new_tag = n_verify_failed = n_skipped = 0
    for row in batch.itertuples():
        mask = (df["item_id"] == row.item_id) & (df["option_value"] == row.option_value)
        if not mask.any():
            n_skipped += 1
            continue
        label = row.gemini_misconception_label
        if row.task == "new_tag":
            new_id = label_to_id[label.lower()]
            df.loc[mask, "misconception_id"] = new_id
            df.loc[mask, "misconception_label"] = label
            df.loc[mask, "final_misconception_label"] = label
            df.loc[mask, "label_source"] = "gemini_new_tag"
            n_new_tag += mask.sum()
        else:  # verify_failed
            df.loc[mask, "final_misconception_label"] = label
            df.loc[mask, "label_source"] = "gemini_verify_retag"
            n_verify_failed += mask.sum()

    df.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"Applied {n_new_tag} new_tag + {n_verify_failed} verify_failed rows "
          f"({n_skipped} rows in batch not found in {args.final}, skipped)")
    print(f"Wrote {len(df):,} rows to {args.out}")
    print(f"New clusters created: {len(label_to_id)}")


if __name__ == "__main__":
    main()
