"""Gemini reviewed gemini_retag_batch_v2.csv (the 150 rows still blank after
merging run 2) and, following the prompt's explicit instruction, left every
row blank rather than force a label -- correctly, since these are mostly
non-numeric/off-topic responses (symbols, stray Finnish words from an
unrelated verb-conjugation exercise, mismatched units) with no derivable
arithmetic error.

For consistency with how run 1's 641 elicit-UNKNOWN rows were handled (all
641 got assigned to some bucket, none left blank -- see
apply_gemini_retag.py / MATH_TAGGING.md), this script buckets these 150 the
same way instead of leaving them permanently untagged, reusing an existing
misconception_id/label wherever the pattern already exists in the catalog:

  - non-numeric / off-topic text (symbols, stray words, letter-choice with
    no context) -> reuse "Provides an empty, missing, or unparseable
    answer." (MC_GEMINI_0027, already used for exactly this pattern 20x)
  - numeric (or algebraic/ratio) answer with no discernible relationship to
    the correct answer -> reuse MC_GEMINI_0001, generalizing its wording
    (dropped "numeric"/"product" so it now also covers the 2 non-numeric-
    but-still-no-relationship rows, e.g. an algebraic expression or a ratio)
  - numeric answer whose UNIT doesn't match the expected quantity (all from
    unit-recognition drills, e.g. answering a time question with "7dl") --
    a genuinely distinct, specific pattern here, not "guesses wildly" --
    gets one new MC_GEMINI_#### id.

Usage:
  python apply_run2_fallback_tags.py
"""
import re
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent

UNPARSEABLE_LABEL = "Provides an empty, missing, or unparseable answer."
NO_RELATIONSHIP_LABEL = "Provides an answer with no discernible mathematical relationship to the correct answer."
UNIT_MISMATCH_LABEL = "Selects a numeric value with an unrelated or mismatched unit of measurement."

UNIT_RE = re.compile(r'^[\-\u2212]?\d+([.,]\d+)?\s*(dl|ltr|cl|kg|g|ms|h|d|e|q|r|b|s)$', re.IGNORECASE)
NUMERIC_RE = re.compile(r'^[\-\u2212]?\d+([.,]\d+)?\s*%?$')
SYMBOLIC_MATH_RE = re.compile(r'^\d+\s*[:\-]\s*\d+$|^[\-\u2212]?\d*[a-z]\s*[\-\u2212+]\s*\d+$', re.IGNORECASE)


def classify(v: str) -> str:
    v = v.strip()
    if UNIT_RE.match(v):
        return "unit_mismatch"
    if NUMERIC_RE.match(v) or SYMBOLIC_MATH_RE.match(v):
        return "no_relationship"
    return "unparseable"


def main():
    v3_path = ROOT / "out" / "distractor_catalog_final_v3.csv"
    out_path = ROOT / "out" / "distractor_catalog_final_v4.csv"
    batch_path = ROOT / "out" / "gemini_retag_batch_v2.csv"

    df = pd.read_csv(v3_path, encoding="utf-8-sig", dtype=str, keep_default_na=False, low_memory=False)
    batch = pd.read_csv(batch_path, encoding="utf-8-sig", dtype=str, keep_default_na=False, low_memory=False)

    # Generalize MC_GEMINI_0001's existing wording so the 16 rows already on
    # it (multiplication-specific "...correct product.") and the new,
    # broader set of rows share one consistent, more general label.
    old_narrow_label = "Provides a numeric answer with no discernible arithmetic relationship to the correct product."
    df.loc[df["final_misconception_label"] == old_narrow_label, "final_misconception_label"] = NO_RELATIONSHIP_LABEL
    df.loc[df["misconception_label"] == old_narrow_label, "misconception_label"] = NO_RELATIONSHIP_LABEL

    id_by_label = {
        "unparseable": UNPARSEABLE_LABEL,
        "no_relationship": NO_RELATIONSHIP_LABEL,
    }
    # Resolve the existing ids for the two reused labels from the catalog itself.
    label_to_id = {}
    for norm_label in {UNPARSEABLE_LABEL, NO_RELATIONSHIP_LABEL}:
        matches = df.loc[df["final_misconception_label"] == norm_label, "misconception_id"]
        if not matches.empty:
            label_to_id[norm_label] = matches.iloc[0]

    existing_ids = set(df["misconception_id"].dropna().astype(str))
    n = 0
    while f"MC_GEMINI_{n:04d}" in existing_ids:
        n += 1
    unit_mismatch_id = f"MC_GEMINI_{n:04d}"
    label_to_id[UNIT_MISMATCH_LABEL] = unit_mismatch_id

    n_by_bucket = {"unparseable": 0, "no_relationship": 0, "unit_mismatch": 0}
    for row in batch.itertuples():
        # is_correct_option == "0" restricts the match to the wrong-answer
        # row: the catalog can have a same-text correct-answer row too (see
        # merge_into_kt.py's docstring) -- a fallback tag must never land on
        # that one.
        mask = ((df["item_id"] == row.item_id) & (df["option_value"] == row.option_value)
                & (df["is_correct_option"] == "0"))
        if not mask.any():
            continue
        bucket = classify(row.students_wrong_answer)
        n_by_bucket[bucket] += 1
        label = {"unparseable": UNPARSEABLE_LABEL, "no_relationship": NO_RELATIONSHIP_LABEL,
                 "unit_mismatch": UNIT_MISMATCH_LABEL}[bucket]
        df.loc[mask, "misconception_id"] = label_to_id[label]
        df.loc[mask, "misconception_label"] = label
        df.loc[mask, "final_misconception_label"] = label
        df.loc[mask, "label_source"] = "gemini_new_tag_run2_fallback"
        df.loc[mask, "tagging_run"] = "run2_gemini_fallback"

    df.to_csv(out_path, index=False, encoding="utf-8-sig")
    print(f"Bucketed {sum(n_by_bucket.values())} rows: {n_by_bucket}")
    print(f"Wrote {len(df):,} rows to {out_path}")
    print(f"Tagged rows now: {(df['final_misconception_label'].str.strip() != '').sum():,}")


if __name__ == "__main__":
    main()
