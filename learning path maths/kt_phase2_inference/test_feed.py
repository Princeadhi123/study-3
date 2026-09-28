"""Observed-answer feed prototype -- midpoint and end-of-test feedback.

This is the first safe slice of the informational feed described in the
README's "Current product goal and scope": a pure function over the
completed-answer prefix of the standard 40-question test -- four skills,
five distinct questions per skill in questions 1-20, and five NEW
distinct questions on the same four skills in questions 21-40.

It reports only what was observed. The conformal gate is calibrated on
10 same-skill attempts and is deliberately NOT applied to these
five-answer skill blocks, so nothing here is a calibrated mastery call.
The feed never redirects a student and never enforces a next step;
`suggestions` stays empty pending graph validation.
"""

HALF_LENGTH = 20
FULL_LENGTH = 40
SKILLS_PER_TEST = 4
QUESTIONS_PER_SKILL_PER_HALF = 5


def build_test_feed(answers: list[dict], skill_names: dict[str, str]) -> dict:
    """Build a deterministic feedback dict from a completed-answer prefix.

    `answers` is the ordered prefix of completed answers: exactly 20 rows
    (midpoint) or exactly 40 rows (end). Each row needs `question_id`
    (unique nonempty string), `skill_id` (nonempty string) and `correct`
    (a real bool). `skill_names` must give a nonempty label for every
    skill present. Raises ValueError on any malformed input.
    """
    if not isinstance(answers, list):
        raise ValueError("answers must be a list of answer rows")
    if len(answers) not in (HALF_LENGTH, FULL_LENGTH):
        raise ValueError(
            f"answers must contain exactly {HALF_LENGTH} or "
            f"{FULL_LENGTH} rows, got {len(answers)}"
        )
    for index, row in enumerate(answers):
        _validate_row(index, row)

    question_ids = [row["question_id"] for row in answers]
    if len(set(question_ids)) != len(question_ids):
        raise ValueError("question_id values must be distinct")

    first_half = answers[:HALF_LENGTH]
    order = _half_skill_order(first_half, "the first 20 answers")
    second_half = None
    if len(answers) == FULL_LENGTH:
        second_half = answers[HALF_LENGTH:]
        second_order = _half_skill_order(second_half, "the second 20 answers")
        if set(second_order) != set(order):
            raise ValueError(
                "the second 20 answers must cover the same four skills "
                "as the first 20"
            )

    if not isinstance(skill_names, dict):
        raise ValueError("skill_names must be a dict of skill_id to label")
    for skill_id in order:
        name = skill_names.get(skill_id)
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"skill_names must give a nonempty label for {skill_id!r}"
            )

    checkpoint = "midpoint" if second_half is None else "end"
    student_skills = []
    teacher_skills = []
    for skill_id in order:
        first_correct = _correct_count(first_half, skill_id)
        out_of = QUESTIONS_PER_SKILL_PER_HALF
        total_correct = first_correct
        teacher_entry = {
            "skill_id": skill_id,
            "skill_name": skill_names[skill_id],
            "first_half": {"correct": first_correct, "out_of": out_of},
        }
        if second_half is not None:
            second_correct = _correct_count(second_half, skill_id)
            teacher_entry["second_half"] = {
                "correct": second_correct,
                "out_of": QUESTIONS_PER_SKILL_PER_HALF,
            }
            total_correct += second_correct
            out_of += QUESTIONS_PER_SKILL_PER_HALF
        teacher_entry["total"] = {"correct": total_correct, "out_of": out_of}
        teacher_skills.append(teacher_entry)
        student_skills.append(
            {
                "skill_id": skill_id,
                "skill_name": skill_names[skill_id],
                "correct": total_correct,
                "out_of": out_of,
                "text": _student_text(
                    skill_names[skill_id], total_correct, out_of
                ),
            }
        )

    if checkpoint == "midpoint":
        summary = (
            "Halfway through: 20 questions answered. Here is how each "
            "topic has gone so far -- information only, no grade and no "
            "next step attached."
        )
    else:
        summary = (
            "All 40 questions answered. Here is how each topic went -- "
            "information only, no grade and no next step attached."
        )

    return {
        "checkpoint": checkpoint,
        "questions_answered": len(answers),
        "student": {"summary": summary, "skills": student_skills},
        "teacher": {
            "skills": teacher_skills,
            "total": {
                "correct": sum(r["correct"] for r in answers),
                "out_of": len(answers),
            },
        },
        "evidence": {
            "source": "observed_answers_only",
            "model_status": "not_evaluated",
            "limitation": (
                "Each skill block is five answers on distinct questions; "
                "five-answer blocks lack calibrated mastery bounds, so "
                "these counts are descriptive only."
            ),
        },
        "suggestions": [],
    }


def _validate_row(index: int, row) -> None:
    if not isinstance(row, dict):
        raise ValueError(f"answers[{index}] must be a dict")
    question_id = row.get("question_id")
    if not isinstance(question_id, str) or not question_id.strip():
        raise ValueError(
            f"answers[{index}].question_id must be a nonempty string"
        )
    skill_id = row.get("skill_id")
    if not isinstance(skill_id, str) or not skill_id.strip():
        raise ValueError(
            f"answers[{index}].skill_id must be a nonempty string"
        )
    if not isinstance(row.get("correct"), bool):
        raise ValueError(f"answers[{index}].correct must be a bool")


def _half_skill_order(rows: list[dict], label: str) -> list[str]:
    order = []
    counts = {}
    for row in rows:
        skill_id = row["skill_id"]
        if skill_id not in counts:
            order.append(skill_id)
            counts[skill_id] = 0
        counts[skill_id] += 1
    if len(order) != SKILLS_PER_TEST:
        raise ValueError(
            f"{label} must cover exactly {SKILLS_PER_TEST} skill IDs, "
            f"got {len(order)}"
        )
    bad = {
        s: c for s, c in counts.items() if c != QUESTIONS_PER_SKILL_PER_HALF
    }
    if bad:
        raise ValueError(
            f"{label} must contain exactly "
            f"{QUESTIONS_PER_SKILL_PER_HALF} answers per skill, got {bad}"
        )
    return order


def _correct_count(rows: list[dict], skill_id: str) -> int:
    return sum(
        1 for r in rows if r["skill_id"] == skill_id and r["correct"]
    )


def _student_text(skill_name: str, correct: int, out_of: int) -> str:
    if correct == out_of:
        return (
            f"All {out_of} questions on {skill_name} were answered "
            "correctly."
        )
    if correct == 0:
        return (
            f"No questions on {skill_name} were answered correctly; "
            "that is an observation, not a verdict."
        )
    return (
        f"{correct} of {out_of} questions on {skill_name} were answered "
        "correctly."
    )
