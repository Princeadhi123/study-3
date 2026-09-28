"""List conservative text-only MCQ candidates for explicit manual selection.

This survey never approves, rewrites, or exposes an existing question bank.
"""
import argparse
import gzip
import json
from collections import defaultdict
from pathlib import Path

import paths


def candidates(inventory: Path, flags_path: Path):
    by_skill = defaultdict(dict)
    excluded = {"possible_missing_visual_or_external_context",
                "markup_or_embedded_media_render_check", "nonskill_label",
                "same_visible_content_conflicting_answer_keys"}
    with gzip.open(inventory, "rt", encoding="utf-8") as questions, gzip.open(
            flags_path, "rt", encoding="utf-8") as flags:
        for line in questions:
            flag_line = flags.readline()
            if not flag_line:
                raise ValueError("Review flags shorter than inventory")
            q, f = json.loads(line), json.loads(flag_line)
            if (q["question_id"], q["skill_id"]) != (f["question_id"], f["skill_id"]):
                raise ValueError("Review flags do not align with inventory")
            if excluded.intersection(f["flags"]) or not q["in_model_catalog"]:
                continue
            skill = by_skill[q["skill_id"]]
            prompt = " ".join(q["text"].split()).casefold()
            if prompt not in skill or q["question_id"] < skill[prompt]["question_id"]:
                skill[prompt] = q
        if flags.readline():
            raise ValueError("Review flags longer than inventory")
    return {skill: list(sorted(rows.values(), key=lambda q: q["question_id"]))
            for skill, rows in by_skill.items()}


def diverse(rows, limit=80):
    by_template = defaultdict(list)
    for q in rows:
        by_template[q["exercise_id"]].append(q)
    output = []
    for position in range(max((len(v) for v in by_template.values()), default=0)):
        for template in sorted(by_template):
            group = by_template[template]
            if position < len(group):
                output.append(group[position])
                if len(output) == limit:
                    return output
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path,
                        default=paths.ARTIFACTS / "mcq_inventory_private.jsonl.gz")
    parser.add_argument("--flags", type=Path,
                        default=paths.ARTIFACTS / "mcq_review_flags_private.jsonl.gz")
    parser.add_argument("--skills", nargs="*", default=[])
    parser.add_argument("--out", type=Path,
                        default=paths.ARTIFACTS / "mcq_candidate_survey.json")
    parser.add_argument("--compact-out", type=Path)
    args = parser.parse_args()
    if args.out.exists() or (args.compact_out and args.compact_out.exists()):
        raise FileExistsError("Refusing to overwrite candidate output")
    by_skill = candidates(paths.require(args.inventory), paths.require(args.flags))
    rankings = sorted(by_skill, key=lambda s: (-len(by_skill[s]), s))
    report = {
        "status": "candidate_survey_only; no item approved",
        "filters": "No visual, markup, nonskill-label or visible-key-conflict flags; in model catalog; one rendering per distinct normalized prompt; imperative, short and two-choice prompts still need manual review",
        "skills_with_ten_prompts": sum(len(rows) >= 10 for rows in by_skill.values()),
        "top_skills": [{"skill_id": skill, "skill_name": by_skill[skill][0]["skill_name"],
                        "distinct_prompts": len(by_skill[skill])} for skill in rankings[:60]],
        "requested_skills": {skill: [{key: q[key] for key in
                                      ("question_id", "exercise_id", "text", "options", "answer_index")}
                                     for q in diverse(by_skill.get(skill, []))]
                             for skill in args.skills},
    }
    with args.out.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    if args.compact_out:
        with args.compact_out.open("x", encoding="utf-8") as stream:
            for skill in args.skills:
                stream.write(f"SKILL {skill}\n")
                for q in report["requested_skills"][skill]:
                    prompt = " ".join(q["text"].split())
                    options = "; ".join(f"{i}:{' '.join(v.split())}"
                                        for i, v in enumerate(q["options"]))
                    stream.write(f"{q['exercise_id']} | {q['question_id']} | {prompt} | "
                                 f"{options} | KEY={q['answer_index']}\n")
    print(f"Found {report['skills_with_ten_prompts']} skills with at least 10 filtered distinct MCQ prompts")
    print(f"Wrote candidate survey to {args.out}")


if __name__ == "__main__":
    main()
