"""Build a small, self-contained CSV for handing to an external chatbot (e.g.
Gemini) to sanity-check the highest-impact flagged misconception assignments.

human_review_queue.csv alone has no question text/correct answer, so a
reviewer (human or chatbot) can't judge whether a label actually fits just
from it. This script joins in the question context from item_context.csv and
takes the top --n rows by impact (times_selected), producing one small CSV
you can upload/paste directly, no need to hand over the whole pipeline.

Usage:
  python make_gemini_review_sample.py --n 50
"""
import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--review-queue", default=str(ROOT / "out" / "human_review_queue.csv"))
    parser.add_argument("--item-context",
                         default=str(ROOT.parent / "learning path maths" / "kt_phase1" / "reports_v2" / "item_context.csv"))
    parser.add_argument("--n", type=int, default=50, help="Top N rows by times_selected to include")
    parser.add_argument("--out", default=str(ROOT / "out" / "gemini_review_sample.csv"))
    args = parser.parse_args()

    rq = pd.read_csv(args.review_queue, encoding="utf-8-sig", low_memory=False)
    rq = rq.sort_values("times_selected", ascending=False).head(args.n)

    ctx = pd.read_csv(args.item_context, encoding="utf-8-sig")[
        ["item_id", "text", "correct_option_value", "exercise_name", "skill_name"]
    ]
    merged = rq.merge(ctx, on="item_id", how="left")

    # Split the verify_verdict into the two pieces a reviewer actually cares
    # about: what the LLM's second pass decided, and (if REPLACE) what it
    # suggested instead.
    merged["verify_decision"] = merged["verify_verdict"].str.extract(r"^(CONFIRM|REPLACE)", expand=False)
    merged["verify_suggested_label"] = merged["verify_verdict"].str.extract(r"REPLACE\s*:\s*(.*)", expand=False)

    out_cols = [
        "item_id", "exercise_type", "skill_name", "exercise_name", "text",
        "correct_option_value", "option_value", "times_selected",
        "misconception_label", "verify_decision", "verify_suggested_label",
        "confidence_margin", "cluster_size",
    ]
    merged = merged[out_cols].rename(columns={
        "text": "question_text",
        "option_value": "students_wrong_answer",
        "misconception_label": "auto_assigned_misconception",
    })
    merged.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"Wrote {len(merged)} rows (top {args.n} by times_selected) to {args.out}")


if __name__ == "__main__":
    main()
