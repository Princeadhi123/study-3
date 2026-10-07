"""Callable MCQ-only serving path for the offline 40-question test.

Unlike simulate_test_feed.py (an offline scripted demo), this module is the
deterministic scoring surface a trusted backend may call: `student_questions`
returns key-free question payloads for one half of the test, and
`score_checkpoint` privately grades an ordered response prefix and returns
only the observed-answer feed from test_feed.build_test_feed. There is no
LLM, no numeric or free-text grading, and no model-based claim anywhere in
this path.

Both entry points require the bank's `protocol` to be
"offline_mcq_demo_only" AND `review_status` to equal exactly "approved";
the built bank ships as "unreviewed", so a designated reviewer must
explicitly mark the assembled bank reviewed/approved offline before
anything here will serve it. Unreviewed or non-MCQ inputs are rejected.

Both `student` and `teacher` views are returned to the trusted backend
caller; the integrating application must role-separate them (students must
never receive the teacher view or the bank JSON, which contains the answer
key). No web-app integration exists yet.
"""
from test_feed import build_test_feed

REQUIRED_PROTOCOL = "offline_mcq_demo_only"
REQUIRED_REVIEW = "approved"
HALF_LENGTH = 20
FULL_LENGTH = 40
SKILLS_PER_TEST = 4
QUESTIONS_PER_SKILL_PER_HALF = 5
PUBLIC_FIELDS = ("question_id", "skill_id", "text", "options")


def student_questions(bank: dict, half: int) -> list[dict]:
    """Return the 20 key-free question dicts for half 1 or 2 of the test.

    `half` must be the integer 1 or 2 (not a bool). Each returned dict has
    only `question_id`, `skill_id`, `text`, and `options` — never
    `answer_index`, `content_text`, `item_id`, or `exercise_id`. Options
    are copied so callers cannot mutate the bank. Raises ValueError on any
    malformed or unapproved input.
    """
    questions = _validate_bank(bank)
    if not isinstance(half, int) or isinstance(half, bool) or half not in (1, 2):
        raise ValueError("half must be the integer 1 or 2")
    start = 0 if half == 1 else HALF_LENGTH
    return [
        {
            "question_id": q["question_id"],
            "skill_id": q["skill_id"],
            "text": q["text"],
            "options": list(q["options"]),
        }
        for q in questions[start:start + HALF_LENGTH]
    ]


def score_checkpoint(bank: dict, responses: list[dict]) -> dict:
    """Grade an ordered 20- or 40-response prefix and return the feed dict.

    `responses` must be the ordered prefix of exactly 20 (midpoint) or 40
    (end) rows, each exactly {'question_id': str, 'selected_index': int},
    with question IDs matching the bank's question order and each
    selected_index a strict non-bool int naming a valid option. Grading
    against the private answer key happens here only; the returned dict is
    exactly build_test_feed's output — no key, no selected text. Raises
    ValueError on any malformed or unapproved input.
    """
    questions = _validate_bank(bank)
    if not isinstance(responses, list):
        raise ValueError("responses must be a list of response rows")
    if len(responses) not in (HALF_LENGTH, FULL_LENGTH):
        raise ValueError(
            f"responses must contain exactly {HALF_LENGTH} or "
            f"{FULL_LENGTH} rows, got {len(responses)}"
        )
    answers = []
    for index, (row, question) in enumerate(zip(responses, questions)):
        if not isinstance(row, dict) or set(row) != {"question_id", "selected_index"}:
            raise ValueError(
                f"responses[{index}] must have exactly the keys "
                "'question_id' and 'selected_index'"
            )
        if row["question_id"] != question["question_id"]:
            raise ValueError(
                f"responses[{index}].question_id must match the bank's "
                f"question order (expected {question['question_id']!r})"
            )
        selected = row["selected_index"]
        if (not isinstance(selected, int) or isinstance(selected, bool)
                or selected not in range(len(question["options"]))):
            raise ValueError(
                f"responses[{index}].selected_index must be an int in "
                f"range({len(question['options'])})"
            )
        answers.append({
            "question_id": question["question_id"],
            "skill_id": question["skill_id"],
            "correct": selected == question["answer_index"],
        })
    return build_test_feed(answers, bank["skill_names"])


def _validate_bank(bank: dict) -> list[dict]:
    """Check protocol, approval, and the full 40-question structure once."""
    if not isinstance(bank, dict):
        raise ValueError("bank must be a dict")
    if bank.get("protocol") != REQUIRED_PROTOCOL:
        raise ValueError(
            f"bank protocol must be {REQUIRED_PROTOCOL!r}; other formats "
            "are not served by this path"
        )
    if bank.get("review_status") != REQUIRED_REVIEW:
        raise ValueError(
            "bank review_status must be exactly 'approved'; a designated "
            "reviewer must mark the assembled bank reviewed offline before "
            "serving"
        )
    return validate_bank_structure(bank)


def validate_bank_structure(bank: dict) -> list[dict]:
    """Structure only; does not approve a bank or authorize learner serving."""
    if not isinstance(bank, dict):
        raise ValueError("bank must be a dict")
    questions = bank.get("questions")
    names = bank.get("skill_names")
    if not isinstance(questions, list) or len(questions) != FULL_LENGTH:
        raise ValueError("bank must contain exactly 40 questions")
    if not isinstance(names, dict):
        raise ValueError("bank must contain a skill_names dict")

    question_ids = []
    prompts = []
    for index, question in enumerate(questions):
        if not isinstance(question, dict):
            raise ValueError(f"questions[{index}] must be a dict")
        question_id = question.get("question_id")
        if not isinstance(question_id, str) or not question_id.strip():
            raise ValueError(
                f"questions[{index}].question_id must be a nonempty string")
        question_ids.append(question_id)
        skill_id = question.get("skill_id")
        name = names.get(skill_id) if isinstance(skill_id, str) else None
        if not isinstance(name, str) or not name.strip():
            raise ValueError(
                f"questions[{index}].skill_id must have a nonempty "
                "skill_names entry")
        exercise_id = question.get("exercise_id")
        if not isinstance(exercise_id, str) or not exercise_id.strip():
            raise ValueError(
                f"questions[{index}].exercise_id must be a nonempty string")
        text = question.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError(f"questions[{index}].text must be nonblank")
        prompts.append(" ".join(text.split()).casefold())
        options = question.get("options")
        answer_index = question.get("answer_index")
        if (not isinstance(options, list) or len(options) < 2
                or any(not isinstance(o, str) or not o.strip() for o in options)
                or len(set(options)) != len(options)):
            raise ValueError(
                f"questions[{index}].options must be at least two "
                "distinct nonblank strings")
        if (not isinstance(answer_index, int) or isinstance(answer_index, bool)
                or answer_index not in range(len(options))):
            raise ValueError(
                f"questions[{index}].answer_index must be an int in "
                f"range({len(options)})")
    if len(set(question_ids)) != FULL_LENGTH:
        raise ValueError("bank question_ids must be 40 distinct values")
    if len(set(prompts)) != FULL_LENGTH:
        raise ValueError(
            "bank must contain 40 distinct visible prompts; repeated "
            "exercise templates are allowed only when the rendered "
            "question text differs")

    halves = (questions[:HALF_LENGTH], questions[HALF_LENGTH:])
    half_skills = []
    for label, rows in (("first", halves[0]), ("second", halves[1])):
        counts = {}
        order = []
        for question in rows:
            skill_id = question["skill_id"]
            if skill_id not in counts:
                order.append(skill_id)
                counts[skill_id] = 0
            counts[skill_id] += 1
        if len(order) != SKILLS_PER_TEST:
            raise ValueError(
                f"the {label} 20 questions must cover exactly "
                f"{SKILLS_PER_TEST} skills, got {len(order)}")
        bad = {s: c for s, c in counts.items()
               if c != QUESTIONS_PER_SKILL_PER_HALF}
        if bad:
            raise ValueError(
                f"the {label} 20 questions must contain exactly "
                f"{QUESTIONS_PER_SKILL_PER_HALF} per skill, got {bad}")
        half_skills.append(set(order))
    if half_skills[0] != half_skills[1]:
        raise ValueError(
            "both halves must cover the same four skills")
    return questions
