"""Build one CSV of every wrong-answer row still unresolved after merging
run 2 (min-times-selected=1) into out/distractor_catalog_final_v3.csv, to
hand to an external chatbot (e.g. Gemini) in one paste -- same pattern as
make_gemini_retag_batch.py used for run 1's residuals.

Scope: wrong-answer rows (is_correct_option == 0) in the two option-based
exercise types (MATH_DRILLER, VILLE_QUIZ) with times_selected >= 1 that are
still blank in final_v3.csv. Two sub-cases, both output as task="new_tag"
since neither has an existing assigned label to compare against:
  - elicit returned an explicit "Misconception: UNKNOWN" (the model
    genuinely found no pattern -- ~141 rows)
  - the elicit call itself failed (empty/truncated response, ~9 rows)

Usage:
  python make_gemini_retag_batch_v2.py
  python make_gemini_retag_batch_v2.py --out out/gemini_retag_batch_v2.csv
"""
import argparse
import json
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent


def load_jsonl_last(path):
    """Last-line-wins per (item_id, option_value) key -- see
    merge_run2_into_final.py's docstring for why (append-only checkpoint)."""
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
    parser.add_argument("--final-v3", default=str(ROOT / "out" / "distractor_catalog_final_v3.csv"))
    parser.add_argument("--run2-checkpoint-dir", default=r"C:\Users\pdaadh\Downloads\checkpoints")
    parser.add_argument("--item-context",
                         default=str(ROOT.parent / "learning path maths" / "kt_phase1" / "reports_v2" / "item_context.csv"))
    parser.add_argument("--out", default=str(ROOT / "out" / "gemini_retag_batch_v2.csv"))
    args = parser.parse_args()

    v3 = pd.read_csv(args.final_v3, encoding="utf-8-sig", dtype=str, keep_default_na=False, low_memory=False)
    v3["times_selected_num"] = pd.to_numeric(v3["times_selected"], errors="coerce")
    blank = v3[(v3["final_misconception_label"].str.strip() == "") & (v3["times_selected_num"] >= 1)
               & (v3["is_correct_option"] == "0") & (v3["exercise_type"].isin(["MATH_DRILLER", "VILLE_QUIZ"]))]

    ctx = pd.read_csv(args.item_context, encoding="utf-8-sig")[
        ["item_id", "text", "correct_option_value", "exercise_name", "skill_name"]
    ]
    ctx_lookup = {r.item_id: r for r in ctx.itertuples(index=False)}
    elicit = load_jsonl_last(str(Path(args.run2_checkpoint_dir) / "elicit.jsonl"))

    rows = []
    n_unknown = n_failed = 0
    for r in blank.itertuples():
        key = f"{r.item_id}\t{r.option_value}"
        rec = elicit.get(key) or {}
        is_unknown = "UNKNOWN" in (rec.get("raw") or "").upper()
        n_unknown += is_unknown
        n_failed += not is_unknown
        c = ctx_lookup.get(r.item_id)
        rows.append({
            "task": "new_tag",
            "item_id": r.item_id,
            "option_value": r.option_value,
            "exercise_type": r.exercise_type,
            "skill_name": getattr(c, "skill_name", "") if c is not None else "",
            "exercise_name": getattr(c, "exercise_name", "") if c is not None else "",
            "question_text": getattr(c, "text", "") if c is not None else "",
            "correct_answer": getattr(c, "correct_option_value", "") if c is not None else "",
            "students_wrong_answer": r.option_value,
            "times_selected": r.times_selected,
            "elicit_status": "explicit_unknown" if is_unknown else "elicit_call_failed",
            "elicit_reasoning": rec.get("reasoning", ""),
            "current_misconception_label": "",
            "alternative_label": "",
        })

    out_df = pd.DataFrame(rows).sort_values("times_selected", ascending=False)
    out_df["gemini_misconception_label"] = ""

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(out_df)} rows to {out_path} ({n_unknown} explicit UNKNOWN / {n_failed} elicit call failures)")
    print("Fill in the gemini_misconception_label column, then run apply_gemini_retag_v2.py")


if __name__ == "__main__":
    main()
