"""Build a private, deterministic MCQ bank for an offline 40-question demonstration.

Only one item rendering per exercise template is eligible. The bank includes an
answer key and must never be served directly to a student. The four automatically
selected skills are a data-availability demo, not a curriculum recommendation.
"""
import argparse
import csv
import gzip
import hashlib
import json
import sys
from pathlib import Path

import paths

csv.field_size_limit(min(sys.maxsize, 2**31 - 1))

REQUIRED_COLUMNS = ("skill_id", "skill_name", "item_id", "item_instance_id",
                    "exercise_id", "exercise_family", "correctness_available",
                    "has_options", "text", "options_json", "correct_option_index",
                    "correct_option_value")


def eligible_question(row):
    if (row["exercise_family"] != "mcq" or row["correctness_available"] != "1"
            or row["has_options"] not in ("True", "true", "1")
            or not row["exercise_id"] or not row["item_id"]
            or not row["item_instance_id"]):
        return None
    text = row["text"]
    if len(text.strip()) < 8 or text.strip().lower().startswith("exercise:"):
        return None
    try:
        options = json.loads(row["options_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(options, list) or len(options) < 2:
        return None
    if any(not isinstance(option, dict) or not isinstance(option.get("value"), str)
           or not option["value"].strip() or not isinstance(option.get("correct"), bool)
           for option in options):
        return None
    values = [option["value"] for option in options]
    correct = [i for i, option in enumerate(options) if option["correct"]]
    if (len(set(values)) != len(values) or len(correct) != 1
            or row["correct_option_index"] != str(correct[0])
            or row["correct_option_value"] != values[correct[0]]):
        return None
    content_text = text + " [OPTIONS] " + " | ".join(values)
    signature = json.dumps([row["item_instance_id"], content_text, correct[0]],
                           ensure_ascii=False, separators=(",", ":"))
    question_id = row["item_instance_id"] + "__o" + hashlib.sha256(
        signature.encode("utf-8")).hexdigest()[:16]
    return {
        "question_id": question_id, "skill_id": row["skill_id"],
        "item_id": row["item_id"], "exercise_id": row["exercise_id"],
        "text": text, "options": values, "answer_index": correct[0],
        "content_text": content_text,
    }


def build_bank(source: Path, catalog: Path, skills: list[str] | None = None) -> dict:
    """Select 4 skills and 10 different templates per skill, with no student rows."""
    with open(catalog, encoding="utf-8-sig", newline="") as stream:
        names = {row["skill_id"]: row["skill_name"] for row in csv.DictReader(stream)
                 if row["vocab_idx"] and row["skill_name"].strip()}
    if skills is not None:
        if len(skills) != 4 or len(set(skills)) != 4 or any(s not in names for s in skills):
            raise ValueError("--skills needs four distinct IDs in the model's skill catalog")
        skill_set = set(skills)
    else:
        skill_set = set(names)
    by_skill = {skill: {} for skill in skill_set}
    with gzip.open(source, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if missing := set(REQUIRED_COLUMNS) - set(reader.fieldnames or []):
            raise ValueError(f"Question source missing columns: {sorted(missing)}")
        for row in reader:
            skill = row["skill_id"]
            if skill not in skill_set:
                continue
            question = eligible_question(row)
            if question is None:
                continue
            template = row["exercise_id"]
            previous = by_skill[skill].get(template)
            if previous is None or question["question_id"] < previous["question_id"]:
                by_skill[skill][template] = question
    if skills is None:
        skills = sorted(by_skill, key=lambda s: (-len(by_skill[s]), s))[:4]
    if any(len(by_skill[s]) < 10 for s in skills):
        raise ValueError("Each chosen skill needs at least 10 eligible MCQ exercise templates")
    selected = {}
    seen_content = set()
    for skill in skills:
        questions = []
        for template in sorted(by_skill[skill]):
            question = by_skill[skill][template]
            if question["content_text"] in seen_content:
                continue
            questions.append(question)
            seen_content.add(question["content_text"])
            if len(questions) == 10:
                break
        if len(questions) < 10:
            raise ValueError(f"Skill {skill} has fewer than 10 distinct question renderings")
        selected[skill] = questions
    ordered = []
    for half in (0, 1):
        for slot in range(5):
            for skill in skills:
                ordered.append(selected[skill][2 * slot + half])
    if len({q["question_id"] for q in ordered}) != 40:
        raise ValueError("Question IDs must be unique across the whole test")
    return {
        "protocol": "offline_mcq_demo_only",
        "review_status": "unreviewed; may depend on missing images or have misleading skill labels",
        "selection": "four eligible skills by template count (or explicit IDs); one rendering per exercise; lexicographic template order, alternated across halves",
        "skill_names": {s: names[s] for s in skills},
        "eligible_templates_by_skill": {s: len(by_skill[s]) for s in skills},
        "questions": ordered,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=paths.INTERACTIONS)
    parser.add_argument("--catalog", type=Path, default=paths.SKILL_CATALOG)
    parser.add_argument("--skills", nargs=4)
    parser.add_argument("--out", type=Path, default=paths.ARTIFACTS / "test_question_bank.json")
    args = parser.parse_args()
    bank = build_bank(paths.require(args.source), paths.require(args.catalog), args.skills)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(bank, stream, ensure_ascii=False, indent=2)
    print(f"Wrote {args.out} with 40 questions for 4 skills (offline demo only)")
    for skill in bank["skill_names"]:
        print(f"  {skill}: {bank['eligible_templates_by_skill'][skill]} eligible templates")


if __name__ == "__main__":
    main()
