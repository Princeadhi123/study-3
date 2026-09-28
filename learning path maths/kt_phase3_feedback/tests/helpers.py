"""Synthetic bank fixture for Phase 3 unit tests."""
SKILLS = ("sA", "sB", "sC", "sD")


def make_bank():
    questions = []
    for half in (0, 1):
        for slot in range(5):
            for skill in SKILLS:
                index = half * 5 + slot
                questions.append({
                    "question_id": f"{skill}__q{index}",
                    "skill_id": skill,
                    "item_id": f"item-{skill}-{index}",
                    "exercise_id": f"ex-{skill}-{index}",
                    "text": f"Prompt {skill} {index}?",
                    "options": ["alpha", "beta", "gamma"],
                    "answer_index": index % 3,
                    "content_text": (
                        f"Prompt {skill} {index}? "
                        "[OPTIONS] alpha | beta | gamma"
                    ),
                })
    return {
        "protocol": "offline_mcq_demo_only",
        "review_status": "approved",
        "skill_names": {skill: f"Skill {skill}" for skill in SKILLS},
        "questions": questions,
    }


def responses(bank, correct=True, count=40):
    rows = []
    for q in bank["questions"][:count]:
        index = q["answer_index"] if correct else (q["answer_index"] + 1) % len(q["options"])
        rows.append({"question_id": q["question_id"], "selected_index": index})
    return rows
