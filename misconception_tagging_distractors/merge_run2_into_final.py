"""Merge the second supercomputer run (min-times-selected=1 across all 4
exercise types, in Downloads/checkpoints + Downloads/out) into the
authoritative out/distractor_catalog_final_v2.csv, producing
out/distractor_catalog_final_v3.csv.

Run 2 re-did elicit -> cluster -> verify from scratch over a much larger
pool (34,232 vs 19,611 elicit calls -- run 1 used --min-times-selected 2,
run 2 used --min-times-selected 1, exposing ~14.6K previously-excluded
singleton distractors). Because clustering is a full recompute and greedy
clustering is order-sensitive (see tag_math_distractors.py's stage_cluster
docstring), re-running with a larger pool reshuffled cluster assignments for
rows that were already tagged+reviewed in run 1 -- that reshuffling is
re-clustering noise, not a considered per-row re-check, and run 2's raw
output hasn't been through apply_review/Gemini/manual-override the way
final_v2.csv has. So this script does NOT blanket-replace run 1's reviewed
labels with run 2's. Concretely, per (item_id, option_value) key:

  1. Row already had a final label in run 1 (19,580 rows) AND is one of the
     15 rows that got a "verify_failed" (empty verdict) in run 1 and were
     manually patched via a generic Gemini fallback label
     ("...or guesses wildly", label_source=gemini_verify_retag): if run 2's
     verify pass produced a REAL (non-empty) verdict this time, adopt it --
     spot-checked and confirmed materially more specific than the generic
     Gemini fallback (see conversation). 13/15 resolved this way; the
     other 2 are still unresolved in run 2 too, so the Gemini fix stands.
  2. Row already had a final label in run 1, not one of the above 15: KEEP
     run 1's final_misconception_label/label_source untouched (re-clustering
     noise, not a real improvement signal).
  3. Row had NO label in run 1 but got a real (non-UNKNOWN) label in run 2
     (the ~14,458 newly-eligible times_selected==1 rows): adopt run 2's
     label, running the same CONFIRM/REPLACE folding apply_review.py uses,
     with label_source suffixed "_run2" for traceability.
  4. Row had no label in either run (still UNKNOWN, or below threshold in
     both): leave blank, as before. This includes the 641 gemini_new_tag
     rows -- run 2's elicit produced UNKNOWN for the same 641 keys, so
     nothing to adopt; the Gemini fix stands.

Adds a `tagging_run` column ("run1" / "run1+run2_verify_retry" /
"run2_new_coverage") on top of the existing `label_source` for traceability.

Finally, canonicalizes `misconception_id`: it's assigned by cluster order
*within a single run*, not by label text, so the same textual label can end
up with different ids depending on which run (or even which verify/Gemini
free-text edit) touched it -- confirmed on this data: 300/6,751 label groups
were already inconsistent within run 1 alone (pre-existing, from verify
REPLACE / Gemini text not being reconciled against existing cluster ids),
and merging run 2's new coverage in raised that to 572/11,110. For every
group of rows sharing the same normalized (trimmed, casefolded)
final_misconception_label, re-point every row's misconception_id at one
canonical id -- preferring an id that already came from a run-1-derived row
(so ids already in use aren't churned) and, in case of a run-1-internal tie,
the id backing the most total times_selected -- and unify
final_misconception_label to that group's most common exact text.

Usage:
  python merge_run2_into_final.py
  python merge_run2_into_final.py \\
      --run1-final out/distractor_catalog_final_v2.csv \\
      --run2-tagged "C:/Users/pdaadh/Downloads/out/distractor_catalog_tagged.csv" \\
      --run2-checkpoint-dir "C:/Users/pdaadh/Downloads/checkpoints" \\
      --out out/distractor_catalog_final_v3.csv
"""
import argparse
import json
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent


def load_jsonl_last(path):
    """Last-line-wins per (item_id, option_value) key -- checkpoint files are
    append-only, so a retried call leaves both the failed and the successful
    attempt in the file; the later line is always the authoritative one
    (same semantics as tagging_common.JsonlCheckpoint.done)."""
    seen = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            seen[f"{rec['item_id']}\t{rec['option_value']}"] = rec
    return seen


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run1-final", default=str(ROOT / "out" / "distractor_catalog_final_v2.csv"))
    parser.add_argument("--run2-tagged", default=r"C:\Users\pdaadh\Downloads\out\distractor_catalog_tagged.csv")
    parser.add_argument("--run2-checkpoint-dir", default=r"C:\Users\pdaadh\Downloads\checkpoints")
    parser.add_argument("--out", default=str(ROOT / "out" / "distractor_catalog_final_v3.csv"))
    args = parser.parse_args()

    old = pd.read_csv(args.run1_final, encoding="utf-8-sig", dtype=str, keep_default_na=False, low_memory=False)
    new = pd.read_csv(args.run2_tagged, encoding="utf-8-sig", dtype=str, keep_default_na=False, low_memory=False)
    old["k"] = old["item_id"] + "\t" + old["option_value"]
    new["k"] = new["item_id"] + "\t" + new["option_value"]
    new_by_key = new.set_index("k")

    new_verify = load_jsonl_last(str(Path(args.run2_checkpoint_dir) / "verify.jsonl"))

    old["tagging_run"] = "run1"
    old_tagged_mask = old["misconception_label"].str.strip() != ""
    old.loc[old_tagged_mask, "tagging_run"] = "run1"

    gemini_retag_mask = old["label_source"] == "gemini_verify_retag"
    n_retag_updated = 0
    for idx in old.index[gemini_retag_mask]:
        k = old.at[idx, "k"]
        verdict = (new_verify.get(k) or {}).get("verdict", "").strip()
        if not verdict:
            continue  # still unresolved in run 2 too -- keep the Gemini fix
        if verdict.upper().startswith("REPLACE"):
            m = re.search(r"REPLACE\s*:\s*(.*)", verdict, flags=re.IGNORECASE | re.DOTALL)
            label = m.group(1).strip() if m else verdict
            old.at[idx, "final_misconception_label"] = label
            old.at[idx, "label_source"] = "verify_replace_run2_retry"
        else:  # CONFIRM -> use run 2's auto-cluster label (the assignment being confirmed)
            run2_row = new_by_key.loc[k] if k in new_by_key.index else None
            label = run2_row["misconception_label"] if run2_row is not None else old.at[idx, "final_misconception_label"]
            old.at[idx, "final_misconception_label"] = label
            old.at[idx, "label_source"] = "verify_confirm_run2_retry"
        old.at[idx, "tagging_run"] = "run1+run2_verify_retry"
        n_retag_updated += 1
    print(f"Updated {n_retag_updated}/{gemini_retag_mask.sum()} gemini_verify_retag rows with run 2's real verdict")

    # New coverage: rows with no run-1 label, but a real (non-UNKNOWN) run-2 label.
    no_old_label_mask = old["misconception_label"].str.strip() == ""
    n_new_coverage = 0
    for idx in old.index[no_old_label_mask]:
        k = old.at[idx, "k"]
        if k not in new_by_key.index:
            continue
        run2_row = new_by_key.loc[k]
        label = run2_row["misconception_label"]
        if not label or not str(label).strip():
            continue  # UNKNOWN in run 2 too (e.g. the 641 gemini_new_tag keys)
        flagged = run2_row["needs_human_review"] == "1"
        verdict = (run2_row["verify_verdict"] or "").strip()
        old.at[idx, "misconception_id"] = run2_row["misconception_id"]
        old.at[idx, "misconception_label"] = label
        old.at[idx, "confidence_margin"] = run2_row["confidence_margin"]
        old.at[idx, "cluster_size"] = run2_row["cluster_size"]
        old.at[idx, "needs_human_review"] = run2_row["needs_human_review"]
        old.at[idx, "verify_verdict"] = verdict
        if flagged and verdict.upper().startswith("REPLACE"):
            m = re.search(r"REPLACE\s*:\s*(.*)", verdict, flags=re.IGNORECASE | re.DOTALL)
            final_label = m.group(1).strip() if m else label
            old.at[idx, "label_source"] = "verify_replace_run2"
        else:
            final_label = label
            old.at[idx, "label_source"] = "verify_confirm_run2" if flagged else "auto_unflagged_run2"
        old.at[idx, "final_misconception_label"] = final_label
        old.at[idx, "tagging_run"] = "run2_new_coverage"
        n_new_coverage += 1
    print(f"Added {n_new_coverage} newly-tagged rows from run 2 (min-times-selected=1 coverage)")

    old = old.drop(columns=["k"])
    old = canonicalize_misconception_ids(old)

    old.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(old):,} rows to {args.out}")
    print(old["tagging_run"].value_counts())
    print(old["label_source"].value_counts(dropna=False))


def canonicalize_misconception_ids(df):
    df = df.copy()
    df["times_selected_num"] = pd.to_numeric(df["times_selected"], errors="coerce").fillna(0)
    label_norm = df["final_misconception_label"].str.strip().str.lower()
    tagged_mask = label_norm != ""
    is_run1 = df["tagging_run"].isin(["run1", "run1+run2_verify_retry"])

    n_groups_changed = 0
    n_rows_changed = 0
    id_map = {}
    label_map = {}
    for norm, idx in df.index[tagged_mask].to_series().groupby(label_norm[tagged_mask]):
        idx = idx.values
        sub = df.loc[idx]
        run1_sub = sub[is_run1.loc[idx]]
        candidates = run1_sub if not run1_sub.empty else sub
        # canonical id = the id backing the most total times_selected among
        # the preferred (run-1-derived, if any) candidates
        weights = candidates.groupby("misconception_id")["times_selected_num"].sum()
        canonical_id = weights.idxmax()
        # canonical label text = most common exact string across ALL rows in the group
        canonical_label = sub["final_misconception_label"].value_counts().idxmax()
        if sub["misconception_id"].nunique() > 1 or sub["final_misconception_label"].nunique() > 1:
            n_groups_changed += 1
            n_rows_changed += int((sub["misconception_id"] != canonical_id).sum())
        id_map[norm] = canonical_id
        label_map[norm] = canonical_label

    df.loc[tagged_mask, "misconception_id"] = label_norm[tagged_mask].map(id_map)
    df.loc[tagged_mask, "final_misconception_label"] = label_norm[tagged_mask].map(label_map)
    df = df.drop(columns=["times_selected_num"])
    print(f"Canonicalized misconception_id: {n_groups_changed} label groups had inconsistent "
          f"ids/text, re-pointed {n_rows_changed} rows to a single canonical id per label")
    return df


if __name__ == "__main__":
    main()
