"""Inventory every distinct scoring-eligible MCQ rendering, privately and without student data.

This is an unreviewed extraction, not a student-facing bank or KT input.
Repeated student events are collapsed; distinct rendered options and answer keys
are retained except where one rendering has contradictory answer keys.
"""
import argparse
import csv
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import paths
from question_bank import REQUIRED_COLUMNS, eligible_question

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def build_inventory(source: Path, catalog: Path) -> tuple[list[dict], dict]:
    with catalog.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if not {"skill_id", "skill_name", "vocab_idx"}.issubset(reader.fieldnames or []):
            raise ValueError("Skill catalog is missing required columns")
        model_skills = {row["skill_id"]: row["skill_name"] for row in reader
                        if row["vocab_idx"] and row["skill_name"].strip()}
    counters = Counter()
    questions = {}
    renderings = defaultdict(set)
    with gzip.open(source, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if missing := set(REQUIRED_COLUMNS) - set(reader.fieldnames or []):
            raise ValueError(f"Question source missing columns: {sorted(missing)}")
        for row in reader:
            counters["source_events"] += 1
            if row["exercise_family"] != "mcq":
                continue
            counters["mcq_events"] += 1
            if not row["skill_id"]:
                counters["missing_skill_events"] += 1
                continue
            question = eligible_question(row)
            if question is None:
                counters["not_scoring_eligible_events"] += 1
                continue
            counters["scoring_eligible_events"] += 1
            identity = (row["skill_id"], question["question_id"])
            if identity not in questions:
                question["skill_name"] = model_skills.get(row["skill_id"], row["skill_name"])
                question["in_model_catalog"] = row["skill_id"] in model_skills
                question["review_status"] = "unreviewed"
                questions[identity] = question
            elif any(questions[identity][field] != question[field]
                     for field in ("item_id", "exercise_id", "text", "options", "answer_index")):
                raise ValueError(f"Conflicting question_id for {identity!r}")
            rendering = (row["skill_id"], row["item_instance_id"],
                         question["text"], tuple(question["options"]))
            renderings[rendering].add(identity)
    ambiguous = set().union(*(ids for ids in renderings.values() if len(ids) > 1))
    counters["ambiguous_renderings"] = sum(len(ids) > 1 for ids in renderings.values())
    counters["excluded_ambiguous_questions"] = len(ambiguous)
    results = [q for key, q in sorted(questions.items()) if key not in ambiguous]
    counters["distinct_scoring_eligible_questions"] = len(results)
    counters["distinct_skills"] = len({q["skill_id"] for q in results})
    counters["questions_in_model_catalog"] = sum(q["in_model_catalog"] for q in results)
    report = {
        "protocol": "private_mcq_inventory_only",
        "review_status": "unreviewed; prompts/options/skill labels may omit images or context",
        "definition": "All distinct scoring-eligible MCQ renderings across all skills; not one per template",
        "counts": dict(sorted(counters.items())),
        "limitations": "Excluded rows fail question_bank.eligible_question; ambiguous answer keys for the same rendered question are excluded. Historical correctness is not student-facing evidence. No human review or KT validation has occurred.",
    }
    return results, report


def verify_inventory(inventory: Path, report_path: Path) -> dict:
    report = json.loads(report_path.read_text(encoding="utf-8"))
    counts = report["counts"]
    if (counts["mcq_events"] != counts.get("missing_skill_events", 0)
            + counts["not_scoring_eligible_events"] + counts["scoring_eligible_events"]):
        raise ValueError("MCQ event totals do not reconcile")
    seen = set()
    skills = set()
    model_count = 0
    with gzip.open(inventory, "rt", encoding="utf-8") as stream:
        for line in stream:
            q = json.loads(line)
            key = q["skill_id"], q["question_id"]
            if key in seen or q["review_status"] != "unreviewed":
                raise ValueError("Duplicate or approved question in private inventory")
            if (not isinstance(q["options"], list) or len(q["options"]) < 2
                    or len(set(q["options"])) != len(q["options"])
                    or type(q["answer_index"]) is not int
                    or q["answer_index"] not in range(len(q["options"]))
                    or set(q) != {"question_id", "skill_id", "item_id", "exercise_id",
                                  "text", "options", "answer_index", "content_text",
                                  "skill_name", "in_model_catalog", "review_status"}):
                raise ValueError("Malformed or unexpected inventory question")
            seen.add(key)
            skills.add(q["skill_id"])
            model_count += q["in_model_catalog"]
    if (len(seen) != counts["distinct_scoring_eligible_questions"]
            or len(skills) != counts["distinct_skills"]
            or model_count != counts["questions_in_model_catalog"]):
        raise ValueError("Inventory records do not match report counts")
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=paths.INTERACTIONS)
    parser.add_argument("--catalog", type=Path, default=paths.SKILL_CATALOG)
    parser.add_argument("--out", type=Path, default=paths.ARTIFACTS / "mcq_inventory_private.jsonl.gz")
    parser.add_argument("--report", type=Path, default=paths.ARTIFACTS / "mcq_inventory_report.json")
    parser.add_argument("--verify", action="store_true", help="Check an existing inventory against its report")
    args = parser.parse_args()
    if args.verify:
        counts = verify_inventory(paths.require(args.out), paths.require(args.report))
        print(f"Verified {counts['distinct_scoring_eligible_questions']:,} private MCQ questions")
        return
    if args.out.exists() or args.report.exists():
        raise FileExistsError("Inventory output or report already exists; refusing to overwrite")
    questions, report = build_inventory(paths.require(args.source), paths.require(args.catalog))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(args.out, "xt", encoding="utf-8") as stream:
        for question in questions:
            stream.write(json.dumps(question, ensure_ascii=False) + "\n")
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(f"Wrote {len(questions):,} private unreviewed MCQ questions to {args.out}")
    print(f"Wrote extraction counts to {args.report}")


if __name__ == "__main__":
    main()
