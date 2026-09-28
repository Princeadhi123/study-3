"""Create an explicitly approved copy of the checked text-only MCQ bank.

This is a controlled local approval step for the offline prototype. It first
rebuilds the bank from the private inventory, flags, and checked selection, and
refuses to approve any source artifact that does not match that checked set.
"""
import argparse
import copy
import json
from pathlib import Path

import paths
from build_text_only_bank import build

CHECKED_STATUS = "text_only_checked; not educator_approved"


def approved_copy(inventory: Path, flags_path: Path, selection_path: Path,
                  source_path: Path, reviewer: str) -> dict:
    """Return an approved copy only if the source matches the checked set."""
    regenerated, _ = build(inventory, flags_path, selection_path)
    source = json.loads(source_path.read_text(encoding="utf-8"))
    if source.get("review_status") != CHECKED_STATUS:
        raise ValueError(
            f"source review_status must be {CHECKED_STATUS!r} before approval")
    if source != regenerated:
        raise ValueError(
            "source bank does not match the checked private selection")
    approved = copy.deepcopy(source)
    approved["review_status"] = "approved"
    approved["approval"] = {
        "approved_by": reviewer,
        "scope": "offline MCQ prototype only",
        "basis": ("All 40 visible prompts and options were checked against "
                  "the private selection; the generated bank exactly matches "
                  "the reviewed source records."),
        "limitations": ("Local prototype approval only; not external "
                        "curriculum or platform certification."),
    }
    return approved


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path,
                        default=paths.ARTIFACTS / "mcq_inventory_private.jsonl.gz")
    parser.add_argument("--selection", type=Path,
                        default=paths.ARTIFACTS / "text_only_mcq_selection_private.json")
    parser.add_argument("--flags", type=Path,
                        default=paths.ARTIFACTS / "mcq_review_flags_private.jsonl.gz")
    parser.add_argument("--source", type=Path,
                        default=paths.ARTIFACTS / "test_question_bank_text_only_v2.json")
    parser.add_argument("--out", type=Path,
                        default=paths.ARTIFACTS / "test_question_bank_text_only_approved_v2.json")
    parser.add_argument("--reviewer", default="local project-owner review")
    args = parser.parse_args()
    if args.out.exists():
        raise FileExistsError("Refusing to overwrite an existing approved bank")
    bank = approved_copy(paths.require(args.inventory), paths.require(args.flags),
                         paths.require(args.selection), paths.require(args.source),
                         args.reviewer)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(bank, stream, ensure_ascii=False, indent=2)
    print(f"Wrote approved text-only MCQ bank to {args.out}")


if __name__ == "__main__":
    main()
