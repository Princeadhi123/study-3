"""Build one CSV of every row the pipeline could NOT resolve on its own, to
hand to an external chatbot (e.g. Gemini) for tagging in one paste, instead
of re-running the elicit/verify LLM stages:

  1. **new_tag** -- eligible wrong-answer rows (times_selected >=
     --min-times-selected) where stage 1 (elicit) got an explicit
     "Misconception: UNKNOWN" from the model, so they never got a
     misconception_label at all. Sourced from
     checkpoints/elicit.jsonl (label is null/missing) joined against
     distractor_catalog_tagged.csv (still has misconception_label blank),
     restricted to rows actually eligible for elicit.
  2. **verify_failed** -- rows flagged for stage 3 (verify) where the verify
     call itself failed (empty response, so checkpoints/verify.jsonl has an
     empty "verdict") rather than genuinely CONFIRMing/REPLACing. These are
     currently silently kept as their original auto-cluster label by
     apply_review.py (blank verdict looks like CONFIRM), which is wrong --
     they were never actually checked. Includes the original assigned label
     and the nearest-alternative label the internal verify prompt would have
     shown, so Gemini has the same information the model would have had.

Output columns are the same for both row types so it's one flat CSV to
paste into a chat: for new_tag rows, current_misconception_label/
alternative_label are blank (nothing to compare against, just tag it from
scratch); for verify_failed rows they're filled in.

Usage:
  python make_gemini_retag_batch.py
  python make_gemini_retag_batch.py --out out/gemini_retag_batch.csv
"""
import argparse
import json
from collections import defaultdict
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent


def load_jsonl(path, dedupe=True):
    """Read a JsonlCheckpoint file. With dedupe=True (default) keeps only the
    LAST record per item_id+option_value key -- the checkpoint file appends a
    new line on retry rather than overwriting, so raw line iteration sees
    failed first attempts that were later retried successfully (the same
    last-wins semantics tagging_common.JsonlCheckpoint.done uses)."""
    recs = []
    seen = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if dedupe and "item_id" in rec and "option_value" in rec:
                seen[f"{rec['item_id']}\t{rec['option_value']}"] = rec
            else:
                recs.append(rec)
    return recs + list(seen.values())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint-dir", default=str(ROOT / "checkpoints"))
    parser.add_argument("--tagged", default=str(ROOT / "out" / "distractor_catalog_tagged.csv"))
    parser.add_argument("--item-context",
                         default=str(ROOT.parent / "learning path maths" / "kt_phase1" / "reports_v2" / "item_context.csv"))
    parser.add_argument("--out", default=str(ROOT / "out" / "gemini_retag_batch.csv"))
    args = parser.parse_args()

    ctx = pd.read_csv(args.item_context, encoding="utf-8-sig")[
        ["item_id", "text", "correct_option_value", "exercise_name", "skill_name"]
    ]
    ctx_lookup = {r.item_id: r for r in ctx.itertuples(index=False)}

    # --- 1. new_tag: elicit records with no label (explicit UNKNOWN) ---
    elicit_recs = load_jsonl(Path(args.checkpoint_dir) / "elicit.jsonl")
    unknown_recs = [r for r in elicit_recs if not r.get("label")]

    new_tag_rows = []
    for r in unknown_recs:
        c = ctx_lookup.get(r["item_id"])
        new_tag_rows.append({
            "task": "new_tag",
            "item_id": r["item_id"],
            "option_value": r["option_value"],
            "exercise_type": r.get("exercise_type", ""),
            "skill_name": getattr(c, "skill_name", "") if c is not None else "",
            "exercise_name": getattr(c, "exercise_name", "") if c is not None else "",
            "question_text": getattr(c, "text", "") if c is not None else "",
            "correct_answer": getattr(c, "correct_option_value", "") if c is not None else "",
            "students_wrong_answer": r["option_value"],
            "times_selected": r.get("times_selected", ""),
            "current_misconception_label": "",
            "alternative_label": "",
        })

    # --- 2. verify_failed: verify records with an empty verdict ---
    verify_recs = load_jsonl(Path(args.checkpoint_dir) / "verify.jsonl")
    failed_verify = [r for r in verify_recs if not (r.get("verdict") or "").strip()]

    cluster_recs = load_jsonl(Path(args.checkpoint_dir) / "cluster.jsonl")
    cluster_lookup = {f"{r['item_id']}\t{r['option_value']}": r for r in cluster_recs}

    # Same "largest other cluster in this exercise_type" proxy stage_verify
    # uses for the alternative label, so Gemini sees what the internal
    # verify prompt would have shown.
    type_label_size = defaultdict(dict)
    for r in cluster_recs:
        sizes = type_label_size[r["exercise_type"]]
        sizes[r["misconception_label"]] = max(sizes.get(r["misconception_label"], 0), r["cluster_size"])

    verify_rows = []
    for r in failed_verify:
        key = f"{r['item_id']}\t{r['option_value']}"
        crec = cluster_lookup.get(key)
        c = ctx_lookup.get(r["item_id"])
        if crec is not None:
            sizes = type_label_size[crec["exercise_type"]]
            others = sorted((l for l in sizes if l != crec["misconception_label"]), key=lambda l: -sizes[l])
            alt_label = others[0] if others else ""
            exercise_type = crec.get("exercise_type", "")
            current_label = crec.get("misconception_label", "")
            times_selected = crec.get("times_selected", "")
        else:
            alt_label, exercise_type, current_label, times_selected = "", "", "", ""
        verify_rows.append({
            "task": "verify_failed",
            "item_id": r["item_id"],
            "option_value": r["option_value"],
            "exercise_type": exercise_type,
            "skill_name": getattr(c, "skill_name", "") if c is not None else "",
            "exercise_name": getattr(c, "exercise_name", "") if c is not None else "",
            "question_text": getattr(c, "text", "") if c is not None else "",
            "correct_answer": getattr(c, "correct_option_value", "") if c is not None else "",
            "students_wrong_answer": r["option_value"],
            "times_selected": times_selected,
            "current_misconception_label": current_label,
            "alternative_label": alt_label,
        })

    out_df = pd.DataFrame(new_tag_rows + verify_rows)
    out_df["times_selected"] = pd.to_numeric(out_df["times_selected"], errors="coerce")
    out_df = out_df.sort_values(["task", "times_selected"], ascending=[True, False])
    out_df["gemini_misconception_label"] = ""

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(out_df)} rows to {out_path} "
          f"({len(new_tag_rows)} new_tag / {len(verify_rows)} verify_failed)")


if __name__ == "__main__":
    main()
