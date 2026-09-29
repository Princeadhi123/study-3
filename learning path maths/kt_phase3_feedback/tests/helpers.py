"""Synthetic bank fixture for Phase 3 unit tests."""
from session_store import bank_fingerprint

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


def make_taxonomy(bank):
    return {
        "schema": "phase3_assessment_taxonomy_draft_v1",
        "status": "approved_for_descriptive_research_prototype",
        "student_feedback_status": "observed_counts_only_no_subtopic_mastery_or_conformal",
        "bank_fingerprint": bank_fingerprint(bank),
        "relation": "is_part_of_not_prerequisite",
        "topics": [{"skill_id": sid, "skill_name": bank["skill_names"][sid],
                    "subtopics": [{"id": f"sub_{sid}", "name": f"Subtopic {sid}",
                                   "question_ids": [q["question_id"] for q in bank["questions"]
                                                    if q["skill_id"] == sid]}]}
                   for sid in SKILLS],
    }


def responses(bank, correct=True, count=40):
    rows = []
    for q in bank["questions"][:count]:
        index = q["answer_index"] if correct else (q["answer_index"] + 1) % len(q["options"])
        rows.append({"question_id": q["question_id"], "selected_index": index})
    return rows
