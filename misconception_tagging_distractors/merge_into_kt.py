"""Join the final misconception-tagged catalog onto the KT interactions
file, by (item_id, selected_option_value == option_value). Adds
misconception_id / final_misconception_label / label_source columns to every
interaction row (NaN where the selected option was correct, or was an
UNKNOWN/never-tagged distractor).

Also matches on correctness (kt.correctness == tagged.is_correct_option),
not just (item_id, option_value): since build_v2_item_level.py splits the
catalog by per-instance correctness (the same option text can be the correct
answer in some randomized item_instance_id renderings and wrong in others --
see MATH_TAGGING.md's "Why the catalog is keyed by item_id" section), a
single (item_id, option_value) pair can now have two catalog rows. Joining on
correctness too ensures a genuinely-correct interaction can never pick up a
wrong-answer's misconception label (or vice versa) just because the two rows
share the same option text.

Usage:
  python merge_into_kt.py \\
      --kt-interactions "../learning path maths/kt_phase1/data_v2/kt_interactions_v2_item_level_sample.csv" \\
      --tagged out/distractor_catalog_final.csv \\
      --out out/kt_interactions_with_misconceptions.csv
"""
import argparse
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).parent


def main():
    parser = argparse.ArgumentParser()
    kt_default = ROOT.parent / "learning path maths" / "kt_phase1" / "data_v2" / "kt_interactions_v2_item_level_sample.csv"
    parser.add_argument("--kt-interactions", default=str(kt_default))
    parser.add_argument("--tagged", default=str(ROOT / "out" / "distractor_catalog_final.csv"))
    parser.add_argument("--out", default=str(ROOT / "out" / "kt_interactions_with_misconceptions.csv"))
    args = parser.parse_args()

    # Read everything as verbatim strings so pandas never coerces values
    # (e.g. correctness '1' -> '1.0', answer_raw 'N/A' -> ''), which would
    # corrupt downstream parsing and change the file content.
    kt = pd.read_csv(args.kt_interactions, encoding="utf-8-sig", dtype=str,
                     keep_default_na=False, low_memory=False)
    tagged = pd.read_csv(args.tagged, encoding="utf-8-sig", dtype=str,
                         keep_default_na=False, low_memory=False)[
        ["item_id", "option_value", "is_correct_option", "misconception_id",
         "final_misconception_label", "label_source"]
    ]
    # '' would join to '' -- empty option values must be unmatchable
    tagged = tagged[tagged["option_value"] != ""]

    # Join key includes correctness (see module docstring) so a row's
    # correctness at its OWN instance -- not just its option text -- decides
    # which of the (up to 2) same-text catalog rows it matches.
    kt["_is_correct"] = kt["correctness"] == "1"
    tagged["_is_correct"] = tagged["is_correct_option"] == "1"

    merged = kt.merge(
        tagged, how="left",
        left_on=["item_id", "selected_option_value", "_is_correct"],
        right_on=["item_id", "option_value", "_is_correct"],
    ).drop(columns=["option_value", "_is_correct", "is_correct_option"])

    # NOT notna() -- kt/tagged were read with keep_default_na=False, so an
    # untagged-but-matched row (e.g. a correct answer, or a wrong answer
    # below --min-times-selected) has misconception_id == "" (a real empty
    # string), which notna() would wrongly count as "tagged". != "" is the
    # correct check for "actually got a real misconception tag".
    n_tagged = (merged["misconception_id"] != "").sum()
    print(f"{len(merged):,} interaction rows; {n_tagged:,} matched a misconception tag "
          f"({n_tagged / len(merged):.2%})")

    merged.to_csv(args.out, index=False, encoding="utf-8-sig")
    print(f"Wrote {args.out}")


if __name__ == "__main__":
    main()
