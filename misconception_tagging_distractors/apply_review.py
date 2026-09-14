"""Produce the final misconception-tagged catalog by resolving every flagged
(needs_human_review=1) row's label, then layering any manual corrections on
top:

  1. Not flagged / never tagged (correct answers, UNKNOWN elicits) -> label
     unchanged (already blank/original in distractor_catalog_tagged.csv).
  2. Flagged, verify_verdict == CONFIRM (or unparseable)          -> keep the
     original (auto-clustered) label.
  3. Flagged, verify_verdict == REPLACE: <text>                   -> swap in
     the verify pass's suggested label.
  4. Any (item_id, option_value) present in --overrides           -> always
     wins, regardless of 1-3 (e.g. corrections from a manual/Gemini spot
     check of the review queue).

Adds a `label_source` column (manual_override / verify_replace /
verify_confirm / unreviewed) so it's always traceable where a final label
came from.

Usage:
  python apply_review.py --overrides manual_overrides.csv
"""
import argparse
from pathlib import Path

import pandas as pd

from tagging_common import label_leaks_instance_value

ROOT = Path(__file__).parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tagged", default=str(ROOT / "out" / "distractor_catalog_tagged.csv"))
    parser.add_argument("--overrides", default=str(ROOT / "manual_overrides.csv"))
    parser.add_argument("--item-context",
                         default=str(ROOT.parent / "learning path maths" / "kt_phase1" / "reports_v2" / "item_context.csv"))
    parser.add_argument("--out", default=str(ROOT / "out" / "distractor_catalog_final.csv"))
    args = parser.parse_args()

    df = pd.read_csv(args.tagged, encoding="utf-8-sig", low_memory=False)

    flagged = df["needs_human_review"] == 1
    verdict = df["verify_verdict"].fillna("")
    is_replace = flagged & verdict.str.upper().str.startswith("REPLACE")
    is_confirm = flagged & ~is_replace

    df["final_misconception_label"] = df["misconception_label"]
    df.loc[is_replace, "final_misconception_label"] = (
        verdict[is_replace].str.extract(r"REPLACE\s*:\s*(.*)", expand=False).str.strip()
    )

    df["label_source"] = ""
    df.loc[is_confirm, "label_source"] = "verify_confirm"
    df.loc[is_replace, "label_source"] = "verify_replace"
    df.loc[~flagged & df["misconception_label"].notna() & (df["misconception_label"] != ""),
           "label_source"] = "auto_unflagged"

    if Path(args.overrides).exists():
        correct_value_by_item = {}
        if Path(args.item_context).exists():
            ctx = pd.read_csv(args.item_context, encoding="utf-8-sig", dtype=str, keep_default_na=False)
            correct_value_by_item = dict(zip(ctx["item_id"], ctx["correct_option_value"]))
        else:
            print(f"Warning: no item-context file at {args.item_context}, "
                  f"cannot check overrides for leaked instance-specific values")

        overrides = pd.read_csv(args.overrides, encoding="utf-8-sig")
        override_map = {(r.item_id, r.option_value): r.corrected_label for r in overrides.itertuples()}
        n_applied = 0
        leak_warnings = []
        for (item_id, option_value), label in override_map.items():
            mask = (df["item_id"] == item_id) & (df["option_value"] == option_value)
            if mask.any():
                leaks = label_leaks_instance_value(label, option_value, correct_value_by_item.get(item_id, ""))
                if leaks:
                    leak_warnings.append((item_id, option_value, label, leaks))
                df.loc[mask, "final_misconception_label"] = label
                df.loc[mask, "label_source"] = "manual_override"
                n_applied += mask.sum()
        print(f"Applied {n_applied} manual overrides from {args.overrides} "
              f"({len(override_map)} rows in overrides file)")
        if leak_warnings:
            print(f"\nWARNING: {len(leak_warnings)} override label(s) quote back this row's own "
                  f"answer value(s) -- likely overfit to one instance instead of stating a general "
                  f"error pattern (see label_leaks_instance_value docstring). Review before trusting:")
            for item_id, option_value, label, leaks in leak_warnings:
                print(f"  [{item_id}] option_value={option_value!r} leaked={leaks!r}\n"
                      f"      label: {label}")
    else:
        print(f"No overrides file at {args.overrides}, skipping manual corrections")

    df.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(df):,} rows to {args.out}")
    print(df["label_source"].value_counts(dropna=False))


if __name__ == "__main__":
    main()
