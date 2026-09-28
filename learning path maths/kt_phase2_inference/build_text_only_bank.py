"""Assemble the explicitly reviewed 40-question MCQ bank from private inventory.

Keys and manually checked expected answers remain private. No item is approved
for student delivery or wired to KT by this script.
"""
import argparse
import gzip
import json
from pathlib import Path

import paths
from review_mcq_inventory import normalized, review_flags

FIELDS = ("question_id", "skill_id", "item_id", "exercise_id", "text", "options",
          "answer_index", "content_text")
BLOCKING_FLAGS = {"possible_missing_visual_or_external_context",
                  "markup_or_embedded_media_render_check", "nonskill_label",
                  "same_visible_content_conflicting_answer_keys"}


def build(inventory: Path, flags_path: Path, selection_path: Path) -> tuple[dict, dict]:
    selection = json.loads(selection_path.read_text(encoding="utf-8"))
    skills = selection["skills"]
    if len(skills) != 4 or any(len(rows) != 10 for rows in skills.values()):
        raise ValueError("Selection must specify four skills with ten questions each")
    picked = {(skill, qid): expected for skill, rows in skills.items()
              for qid, expected in rows}
    if len(picked) != 40 or len({qid for _, qid in picked}) != 40:
        raise ValueError("All forty question IDs must be distinct")
    found = {}
    with gzip.open(inventory, "rt", encoding="utf-8") as stream, gzip.open(
            flags_path, "rt", encoding="utf-8") as flags:
        for line in stream:
            flag_line = flags.readline()
            if not flag_line:
                raise ValueError("Review flags shorter than inventory")
            q, f = json.loads(line), json.loads(flag_line)
            qid = q["question_id"]
            identity = (q["skill_id"], qid)
            if identity != (f["skill_id"], f["question_id"]):
                raise ValueError("Review flags do not align with inventory")
            if identity not in picked:
                continue
            expected = picked[identity]
            if identity in found or not q["in_model_catalog"]:
                raise ValueError(f"Duplicate or missing model skill: {identity}")
            if q["options"][q["answer_index"]] != expected:
                raise ValueError(f"Answer key disagrees with independent review: {qid}")
            if BLOCKING_FLAGS.intersection(f["flags"]) or BLOCKING_FLAGS.intersection(review_flags(q)):
                raise ValueError(f"Visual/markup/nonskill/key conflict in selected question: {qid}")
            found[identity] = q
        if flags.readline():
            raise ValueError("Review flags longer than inventory")
    if set(found) != set(picked):
        raise ValueError(f"Missing selected IDs: {sorted(set(picked) - set(found))}")
    ordered_skills = list(skills)
    selected = {skill: [found[(skill, qid)] for qid, _ in rows] for skill, rows in skills.items()}
    questions = [selected[skill][2 * slot + half] for half in (0, 1)
                 for slot in range(5) for skill in ordered_skills]
    prompts = [normalized(q["text"]) for q in questions]
    if len(set(prompts)) != 40:
        raise ValueError("Test must have forty distinct visible prompts, not just IDs/options")
    bank = {
        "protocol": "offline_mcq_demo_only",
        "review_status": "text_only_checked; not educator_approved",
        "selection": "Four catalog skills, ten independently checked text-solvable source MCQs per skill; unique prompt per question, repeated templates allowed only for distinct renderings",
        "skill_names": {skill: selected[skill][0]["skill_name"] for skill in ordered_skills},
        "questions": [{field: q[field] for field in FIELDS} for q in questions],
    }
    report = {
        "status": "text_only_checked; not educator_approved; no media used or needed for selected prompts",
        "question_count": 40,
        "distinct_visible_prompts": len(set(prompts)),
        "skill_count": 4,
        "repeated_template_count": 40 - len({q["exercise_id"] for q in questions}),
        "review_limit": "Checks compare the source's option keys against 40 independently calculated/checked answers and reject visual/markup indicators. A curriculum owner must still approve before student delivery or KT claims.",
        "questions": [{"position": index, "question_id": q["question_id"],
                       "skill_id": q["skill_id"], "exercise_id": q["exercise_id"],
                       "prompt": q["text"],
                       "checked_answer": q["options"][q["answer_index"]]}
                      for index, q in enumerate(questions, 1)],
    }
    return bank, report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path,
                        default=paths.ARTIFACTS / "mcq_inventory_private.jsonl.gz")
    parser.add_argument("--selection", type=Path,
                        default=paths.ARTIFACTS / "text_only_mcq_selection_private.json")
    parser.add_argument("--flags", type=Path,
                        default=paths.ARTIFACTS / "mcq_review_flags_private.jsonl.gz")
    parser.add_argument("--out", type=Path,
                        default=paths.ARTIFACTS / "test_question_bank_text_only_v2.json")
    parser.add_argument("--report", type=Path,
                        default=paths.ARTIFACTS / "text_only_40_review_v2.json")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if not args.verify and (args.out.exists() or args.report.exists()):
        raise FileExistsError("Refusing to overwrite an existing private bank or report")
    bank, report = build(paths.require(args.inventory), paths.require(args.flags),
                         paths.require(args.selection))
    if args.verify:
        if (bank != json.loads(paths.require(args.out).read_text(encoding="utf-8"))
                or report != json.loads(paths.require(args.report).read_text(encoding="utf-8"))):
            raise ValueError("Generated bank or review does not match the checked selection")
        print("Verified 40 distinct text-only selected MCQs; still not educator-approved")
        return
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(bank, stream, ensure_ascii=False, indent=2)
    with args.report.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(f"Wrote {len(bank['questions'])} text-only unapproved MCQs to {args.out}")


if __name__ == "__main__":
    main()
