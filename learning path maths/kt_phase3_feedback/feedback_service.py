"""Compose observed MCQ feedback with an explicitly labeled KT estimate."""
import copy
import json
from pathlib import Path

import phase3_paths  # adds Phase 2 to sys.path before its modules import `paths`
from kt_adapter import trace_responses
from mcq_test import score_checkpoint
from session_store import bank_fingerprint


def validate_assessment_taxonomy(bank: dict, taxonomy: dict) -> None:
    if (taxonomy.get("schema") != "phase3_assessment_taxonomy_draft_v1"
            or taxonomy.get("status") != "approved_for_descriptive_research_prototype"
            or taxonomy.get("relation") != "is_part_of_not_prerequisite"
            or taxonomy.get("student_feedback_status") !=
            "observed_counts_only_no_subtopic_mastery_or_conformal"
            or taxonomy.get("bank_fingerprint") != bank_fingerprint(bank)):
        raise ValueError("assessment taxonomy must match the approved bank and descriptive scope")
    questions = {q["question_id"]: q for q in bank["questions"]}
    seen, subtopic_ids, skills = set(), set(), set()
    for topic in taxonomy["topics"]:
        sid = topic["skill_id"]
        if (sid in skills or bank["skill_names"].get(sid) != topic["skill_name"]
                or not isinstance(topic.get("subtopics"), list) or not topic["subtopics"]):
            raise ValueError("assessment taxonomy topic does not match bank skill")
        skills.add(sid)
        for subtopic in topic["subtopics"]:
            sub_id = subtopic["id"]
            if (not isinstance(sub_id, str) or not sub_id or sub_id in subtopic_ids
                    or not isinstance(subtopic.get("name"), str)
                    or not subtopic["name"].strip()
                    or not subtopic.get("question_ids")):
                raise ValueError("assessment taxonomy has an invalid subtopic")
            subtopic_ids.add(sub_id)
            for qid in subtopic["question_ids"]:
                if qid in seen or qid not in questions or questions[qid]["skill_id"] != sid:
                    raise ValueError("assessment taxonomy has duplicate, unknown, or misplaced question")
                seen.add(qid)
    if seen != set(questions) or skills != set(bank["skill_names"]):
        raise ValueError("assessment taxonomy must cover each bank question and skill exactly once")


def load_assessment_taxonomy(bank: dict, path: Path | None = None,
                             required: bool = False) -> dict | None:
    path = Path(path or phase3_paths.ASSESSMENT_TAXONOMY)
    if not path.exists():
        if required:
            raise FileNotFoundError(f"Assessment taxonomy not found: {path}")
        return None
    taxonomy = json.loads(path.read_text(encoding="utf-8"))
    if taxonomy.get("bank_fingerprint") != bank_fingerprint(bank):
        if required:
            raise ValueError("assessment taxonomy bank fingerprint does not match")
        return None
    validate_assessment_taxonomy(bank, taxonomy)
    return taxonomy


def observed_subtopic_counts(taxonomy: dict, rows: list[dict]) -> list[dict]:
    answered = {}
    for row in rows:
        qid = row["question_id"]
        if (qid in answered or not isinstance(row["correct"], bool)):
            raise ValueError("subtopic counts require distinct questions and observed correctness")
        answered[qid] = row
    result = []
    for topic in taxonomy["topics"]:
        for subtopic in topic["subtopics"]:
            questions = [answered[qid] for qid in subtopic["question_ids"] if qid in answered]
            if questions:
                result.append({"skill_id": topic["skill_id"],
                               "skill_name": topic["skill_name"],
                               "subtopic_id": subtopic["id"],
                               "subtopic_name": subtopic["name"],
                               "correct": sum(row["correct"] for row in questions),
                               "out_of": len(questions)})
    if sum(row["out_of"] for row in result) != len(rows):
        raise ValueError("observed rows contain questions outside the assessment taxonomy")
    return result


def attach_observed_subtopics(feed: dict, bank: dict, responses: list[dict],
                              taxonomy: dict) -> None:
    rows = [{"question_id": row["question_id"],
             "correct": row["selected_index"] == question["answer_index"]}
            for row, question in zip(responses, bank["questions"])]
    counts = observed_subtopic_counts(taxonomy, rows)
    if ({"correct": sum(row["correct"] for row in counts),
         "out_of": sum(row["out_of"] for row in counts)} != feed["teacher"]["total"]):
        raise ValueError("subtopic observed counts disagree with validated scoring")
    feed["student"]["subtopics"] = counts
    feed["teacher"]["subtopics"] = copy.deepcopy(counts)


def assessment_feedback_graph(bank: dict, taxonomy: dict, responses: list[dict]) -> dict:
    from schemas import validate_submission
    validate_assessment_taxonomy(bank, taxonomy)
    if len(responses) not in (20, 40):
        raise ValueError("assessment graph requires an ordered 20/40 response checkpoint")
    rows = []
    for i, response in enumerate(responses):
        question = bank["questions"][i]
        selected = validate_submission(response, question, i)
        rows.append({"question_id": question["question_id"],
                     "correct": selected == question["answer_index"]})
    counts = {row["subtopic_id"]: row for row in observed_subtopic_counts(taxonomy, rows)}
    topics, recommendations = [], {}
    for topic in taxonomy["topics"]:
        leaves = []
        for subtopic in topic["subtopics"]:
            scored = counts.get(subtopic["id"], {"correct": 0, "out_of": 0})
            leaves.append({"subtopic_id": subtopic["id"], "subtopic_name": subtopic["name"],
                           "correct": scored["correct"], "out_of": scored["out_of"],
                           "incorrect": scored["out_of"] - scored["correct"]})
        topics.append({"skill_id": topic["skill_id"], "skill_name": topic["skill_name"],
                       "correct": sum(leaf["correct"] for leaf in leaves),
                       "out_of": sum(leaf["out_of"] for leaf in leaves), "subtopics": leaves})
        errors = [copy.deepcopy(leaf) for leaf in leaves if leaf["incorrect"] > 0]
        names = ", ".join(leaf["subtopic_name"] for leaf in errors)
        recommendations[topic["skill_id"]] = {
            "skill_id": topic["skill_id"], "skill_name": topic["skill_name"],
            "recommendation_type": "ASSESSMENT_SUBTOPIC_PRACTICE" if errors else "NONE",
            "subtopics": errors,
            "message": (f"Observed incorrect answers occurred in {names}. Consider reviewing these "
                        "assessed subtopics; the counts do not diagnose a misconception." if errors else
                        "No incorrect answers were observed in the answered questions for this skill.")}
    return {"schema": "phase3_assessment_feedback_graph_v1",
            "status": "descriptive_observed_counts_only",
            "scope": "approved_bank_topics_subtopics_only",
            "relation": "is_part_of_not_prerequisite", "bank_sha256": bank_fingerprint(bank),
            "checkpoint": "midpoint" if len(rows) == 20 else "end",
            "topics": topics, "recommendations": recommendations,
            "recommendation_status": "descriptive_practice_candidates_pending_educator_review"}


def checkpoint_result(bank: dict, responses: list[dict],
                      include_kt: bool = False, kt=None,
                      device: str = "cpu", taxonomy: dict | None = None) -> dict:
    """Return the private scoring result plus an optional frozen-model trace.

    `observed_feed` remains the authoritative feedback from submitted answers.
    `model_estimate` is metadata for a researcher/teacher-facing prototype and
    is deliberately not merged into correctness or mastery text.
    """
    result = {
        "observed_feed": score_checkpoint(bank, responses),
        "model_estimate": {"status": "not_requested"},
    }
    taxonomy = taxonomy if taxonomy is not None else load_assessment_taxonomy(bank)
    if taxonomy is not None:
        validate_assessment_taxonomy(bank, taxonomy)
        attach_observed_subtopics(result["observed_feed"], bank, responses, taxonomy)
    if include_kt:
        result["model_estimate"] = trace_responses(
            bank, responses, kt=kt, device=device)
    return result


_MIDPOINT_QUESTIONS = 20
_END_QUESTIONS = 40
_SKILLS_PER_ASSESSMENT = 4
_ALLOWED_STYLES = {"plain", "warm"}


def compose_student_message(observed_feed: dict, style_selector=None) -> dict:
    """Compose a bounded student-facing message from the observed feed only."""
    checkpoint, skills = _validated_skills(observed_feed)
    context = _selector_context(checkpoint, skills)
    style = "plain"
    bounded = False
    if style_selector is not None:
        try:
            choice = style_selector(copy.deepcopy(context))
        except Exception:
            choice = None
        if (isinstance(choice, dict) and set(choice) == {"style"}
                and isinstance(choice["style"], str)
                and choice["style"] in _ALLOWED_STYLES):
            style = choice["style"]
            bounded = True
    return {
        "message": _message_text(
            checkpoint, style, context["observed_performance"], skills),
        "message_source": "bounded_style" if bounded else "deterministic",
    }


def _validated_skills(observed_feed: dict) -> tuple[str, list[dict]]:
    if not isinstance(observed_feed, dict):
        raise ValueError("observed_feed must be a dict")
    checkpoint = observed_feed.get("checkpoint")
    if checkpoint not in ("midpoint", "end"):
        raise ValueError(
            "observed_feed checkpoint must be 'midpoint' or 'end'")
    student = observed_feed.get("student")
    if not isinstance(student, dict) or not isinstance(
            student.get("skills"), list):
        raise ValueError("observed_feed student must contain a skills list")
    skills = []
    for index, row in enumerate(student["skills"]):
        if not isinstance(row, dict):
            raise ValueError(f"student skills[{index}] must be a dict")
        skill_id = row.get("skill_id")
        name = row.get("skill_name")
        correct = row.get("correct")
        out_of = row.get("out_of")
        if not isinstance(skill_id, str) or not skill_id:
            raise ValueError(f"student skills[{index}] needs a skill_id")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"student skills[{index}] needs a skill_name")
        if (not isinstance(correct, int) or isinstance(correct, bool)
                or not isinstance(out_of, int) or isinstance(out_of, bool)
                or out_of <= 0 or not 0 <= correct <= out_of):
            raise ValueError(
                f"student skills[{index}] has invalid correct/out_of counts")
        skills.append({
            "skill_id": skill_id,
            "display_name": name,
            "correct": correct,
            "total": out_of,
        })
    if not skills:
        raise ValueError("observed_feed student skills must be nonempty")
    if (len(skills) != _SKILLS_PER_ASSESSMENT
            or len({s["skill_id"] for s in skills}) != len(skills)):
        raise ValueError(
            "observed_feed student skills must list exactly "
            f"{_SKILLS_PER_ASSESSMENT} distinct skills")
    answered = (_MIDPOINT_QUESTIONS if checkpoint == "midpoint"
                else _END_QUESTIONS)
    expected_out_of = answered // _SKILLS_PER_ASSESSMENT
    for index, skill in enumerate(skills):
        if skill["total"] != expected_out_of:
            raise ValueError(
                f"student skills[{index}] out_of must be "
                f"{expected_out_of} at {checkpoint}")
    return checkpoint, skills


def _selector_context(checkpoint: str, skills: list[dict]) -> dict:
    total = _MIDPOINT_QUESTIONS if checkpoint == "midpoint" else _END_QUESTIONS
    performance = {"total_questions": total}
    growth = None
    if checkpoint == "end":
        performance["total_correct"] = sum(s["correct"] for s in skills)
        performance["strongest_skill"] = _unique_max_skill(skills)
        growth = _unique_min_skill(skills)
        performance["growth_skill"] = growth
    return {
        "checkpoint": total,
        "observed_performance": performance,
        "prerequisite_guidance": {
            "has_approved_prerequisite": False,
            "prerequisite_name": None,
            "recommendation_type": (
                "DIRECT_SKILL_REVIEW" if growth else "NONE"),
        },
    }


def _unique_max_skill(skills: list[dict]) -> dict | None:
    best = max(s["correct"] for s in skills)
    hits = [s for s in skills if s["correct"] == best]
    if best > 0 and len(hits) == 1:
        return dict(hits[0])
    return None


def _unique_min_skill(skills: list[dict]) -> dict | None:
    low = min(s["correct"] for s in skills)
    hits = [s for s in skills if s["correct"] == low]
    if len(hits) == 1 and hits[0]["correct"] < hits[0]["total"]:
        return dict(hits[0])
    return None


def _name_list(skills: list[dict]) -> str:
    names = [s["display_name"] for s in skills]
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def _message_text(checkpoint: str, style: str, performance: dict,
                  skills: list[dict]) -> str:
    warm = style == "warm"
    if checkpoint == "midpoint":
        first = ("Nice work completing 20 questions." if warm
                 else "You have completed 20 questions.")
        return (f"{first} You are halfway through the assessment. "
                "Take a moment if you need one, then continue when you "
                "are ready.")
    first = ("Nice work completing all 40 questions." if warm
             else "You have completed all 40 questions.")
    if all(s["correct"] == s["total"] for s in skills):
        return (f"{first} You answered every question correctly on this "
                "assessment. Keep building on this with further "
                "practice.")
    if all(s["correct"] == 0 for s in skills):
        return (f"{first} None of the answers on this assessment were "
                "correct. Start with a short worked example in one of "
                "the topics below, then try a similar question. This "
                "result is a starting point, not a verdict on what you "
                "can learn.")
    if len({s["correct"] for s in skills}) == 1:
        return (f"{first} Your observed scores were equal across the "
                "topics. Choose one topic below to practice next, then "
                "try a short set of questions.")
    strongest = performance["strongest_skill"]
    if strongest:
        second = (f"On {strongest['display_name']}, you answered "
                  f"{strongest['correct']} of {strongest['total']} "
                  "questions correctly.")
    else:
        best = max(s["correct"] for s in skills)
        tied = [s for s in skills if s["correct"] == best]
        second = (f"Your highest observed scores were on "
                  f"{_name_list(tied)} ({best} of {tied[0]['total']} "
                  "correct in each).")
    growth = performance["growth_skill"]
    if growth:
        third = (f"For your next step, practice more questions on "
                 f"{growth['display_name']} ({growth['correct']} of "
                 f"{growth['total']} correct).")
    else:
        low = min(s["correct"] for s in skills)
        tied = [s for s in skills if s["correct"] == low]
        third = (f"For your next step, choose one of {_name_list(tied)} "
                 f"to practice ({low} of {tied[0]['total']} correct in "
                 "each).")
    return f"{first} {second} {third}"
