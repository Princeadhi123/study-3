"""Synthetic bank fixture for Phase 3 unit tests."""
import unittest
from pathlib import Path

import phase3_paths
from session_store import bank_fingerprint

SKILLS = ("sA", "sB", "sC", "sD")

# Original frozen research inputs: the v3 capture, its v2 parent
# graph, the warm/cold shadow-smoke banks it binds, and the original
# 24-question v2 practice pool. Recovered copies must be activated
# explicitly; tests requiring the originals skip when they are absent.
FROZEN_RESEARCH_FILES = (
    phase3_paths.ARTIFACTS / "bounded_content_graph_v3_20261007"
    / "content_graph_private.json",
    phase3_paths.ARTIFACTS / "bounded_content_graph_v3_20261007"
    / "manifest.json",
    phase3_paths.ARTIFACTS / "bounded_content_graph_v2_20261006"
    / "content_graph_private.json",
    phase3_paths.ARTIFACTS / "shadow_smoke_20261006"
    / "warm_bank_private.json",
    phase3_paths.ARTIFACTS / "shadow_smoke_20261006"
    / "cold_bank_private.json",
    phase3_paths.ARTIFACTS / "synthetic_practice_supplement_20261006"
    / "practice_pool_v2_private.json",
)


def requires_private_artifacts(*paths):
    """Skip when a required private file is absent; preserve
    validation failures."""
    missing = [str(path) for path in paths
               if not Path(path).is_file()]
    return unittest.skipUnless(
        not missing,
        "private frozen artifacts not installed: " + ", ".join(missing))


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
