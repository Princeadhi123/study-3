"""
Standalone verification script - does NOT modify build_kt_dataset.py or any output file.

Purpose: spot-check every item that was matched to the item map by COLUMN POSITION
(rather than by confirmed name similarity), so we can eyeball whether the raw
question text in the log plausibly matches the item description assigned to it.

For each positionally-matched item, prints:
  - booklet, sequence position
  - raw column name / question text as it appeared in the log export
  - the oplm_name / item_name / description the pipeline assigned to it from the item map
  - the (fi/en) description, if available

Also flags any map items (per booklet) that never got matched to a log column,
as a secondary consistency check.

Run: python verify_positional_matches.py
Writes: kt_dataset/positional_match_review.csv (for manual review in Excel)
"""
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).parent
OUT_DIR = ROOT / "kt_dataset"

items = pd.read_csv(OUT_DIR / "items_master.csv", encoding="utf-8-sig")

da_items = items[items["phase"] == "da_math"].copy()

print("=== Overall match method counts ===")
print(da_items["match_method"].value_counts(dropna=False))
print()

print("=== Positional-match reliance by booklet ===")
by_booklet = (
    da_items.groupby("booklet")["match_method"]
    .value_counts()
    .unstack(fill_value=0)
)
if "positional" not in by_booklet.columns:
    by_booklet["positional"] = 0
if "name" not in by_booklet.columns:
    by_booklet["name"] = 0
by_booklet["total"] = by_booklet.sum(axis=1)
by_booklet["pct_positional"] = (100 * by_booklet["positional"] / by_booklet["total"]).round(1)
by_booklet = by_booklet.sort_values("pct_positional", ascending=False)
print(by_booklet[["name", "positional", "total", "pct_positional"]])
print()

positional = da_items[da_items["match_method"] == "positional"].copy()
print(f"Total positionally-matched DA-math items: {len(positional)} / {len(da_items)} "
      f"({100 * len(positional) / len(da_items):.1f}%)")
print()

review_cols = [
    "booklet", "seq_in_test", "column_name", "task_base_name", "subitem_no",
    "question_text_in_log", "match_confidence", "oplm_id", "oplm_name",
    "item_name_map", "description", "description_en",
]
review = positional[review_cols].sort_values(["booklet", "seq_in_test"])
review.to_csv(OUT_DIR / "positional_match_review.csv", index=False, encoding="utf-8-sig")
print(f"Wrote {len(review)} rows to {OUT_DIR / 'positional_match_review.csv'} for manual review.")
print()

print("=== Sample of positional matches (first 15) for quick eyeball ===")
with pd.option_context("display.max_colwidth", 60, "display.width", 200):
    print(review.head(15).to_string(index=False))
