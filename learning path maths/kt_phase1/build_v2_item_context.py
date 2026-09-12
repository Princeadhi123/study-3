"""Stage 4 (v2, optional): build a per-item_id text/context lookup for the
LLM misconception-tagging pipeline in ../misconception_tagging/.

Why this is needed: reports_v2/distractor_catalog.csv (built by
build_v2_item_level.py) intentionally stores only item_id/option_value/counts
-- not the question text -- to stay small instead of repeating the same text
thousands of times. Misconception tagging needs the actual question text and
the correct answer to reason about *why* a wrong answer was chosen, so this
stage does one more streaming pass over kt_interactions_v2_item_level.csv.gz,
restricted to the option-bearing families (mcq/single_answer_text -- the only
families distractor_catalog.csv covers), and keeps the most common
(text, correct_option_value) pair seen per item_id (a small number of
generated question-text variants can share the same item_id; the most
frequent phrasing is used as that item's representative text for tagging).

Writes reports_v2/item_context.csv:
  item_id, exercise_type, skill_name, exercise_name, text, correct_option_value

Run after build_v2_item_level.py (stage 3), before the tagging scripts in
../misconception_tagging/tag_math_distractors.py.
"""
import csv
import gzip
from collections import Counter, defaultdict
from pathlib import Path

OUT = Path(__file__).parent
DATA = OUT / "data_v2"
REPORTS = OUT / "reports_v2"
SOURCE = DATA / "kt_interactions_v2_item_level.csv.gz"
DEST = REPORTS / "item_context.csv"
OPTION_FAMILIES = {"mcq", "single_answer_text"}
FIELDS = ["item_id", "exercise_type", "skill_name", "exercise_name", "text", "correct_option_value"]


def main():
    # Only option-bearing rows are tracked, so memory stays bounded to the
    # (thousands of) distinct items that actually appear in
    # distractor_catalog.csv, not the full 13.9M-row stream.
    text_counts = defaultdict(Counter)  # item_id -> Counter[(text, correct_option_value)]
    meta = {}  # item_id -> (exercise_type, skill_name, exercise_name)
    rows_seen = 0
    kept = 0

    with gzip.open(SOURCE, "rt", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows_seen += 1
            if row["exercise_family"] in OPTION_FAMILIES:
                item_id = row["item_id"]
                text_counts[item_id][(row["text"], row["correct_option_value"])] += 1
                meta[item_id] = (row["exercise_type"], row["skill_name"], row["exercise_name"])
                kept += 1
            if rows_seen % 3_000_000 == 0:
                print(f"...item-context pass: {rows_seen:,} rows ({kept:,} option-bearing)", flush=True)

    rows_out = []
    for item_id, counter in text_counts.items():
        (text, correct_value), _count = counter.most_common(1)[0]
        exercise_type, skill_name, exercise_name = meta[item_id]
        rows_out.append({
            "item_id": item_id,
            "exercise_type": exercise_type,
            "skill_name": skill_name,
            "exercise_name": exercise_name,
            "text": text,
            "correct_option_value": correct_value,
        })
    rows_out.sort(key=lambda r: r["item_id"])

    REPORTS.mkdir(exist_ok=True)
    with DEST.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows_out)
    print(f"Scanned {rows_seen:,} rows ({kept:,} option-bearing). Wrote {len(rows_out):,} item contexts to {DEST}")


if __name__ == "__main__":
    main()
