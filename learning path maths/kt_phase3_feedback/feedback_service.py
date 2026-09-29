"""Compose observed MCQ feedback with an explicitly labeled KT estimate."""
import copy

import phase3_paths  # adds Phase 2 to sys.path before its modules import `paths`
from kt_adapter import trace_responses
from mcq_test import score_checkpoint


def checkpoint_result(bank: dict, responses: list[dict],
                      include_kt: bool = False, kt=None,
                      device: str = "cpu") -> dict:
    """Return the private scoring result plus an optional frozen-model trace.

    `observed_feed` remains the authoritative feedback from submitted answers.
    `model_estimate` is metadata for a researcher/teacher-facing prototype and
    is deliberately not merged into correctness or mastery text.
    """
    result = {
        "observed_feed": score_checkpoint(bank, responses),
        "model_estimate": {"status": "not_requested"},
    }
    if include_kt:
        result["model_estimate"] = trace_responses(
            bank, responses, kt=kt, device=device)
    return result


_MIDPOINT_QUESTIONS = 20
_END_QUESTIONS = 40
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
            checkpoint, style, context["observed_performance"]),
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


def _message_text(checkpoint: str, style: str, performance: dict) -> str:
    warm = style == "warm"
    if checkpoint == "midpoint":
        first = ("Nice work completing 20 questions." if warm
                 else "You have completed 20 questions.")
        return (f"{first} You are halfway through the assessment. "
                "Continue when you are ready.")
    first = ("Nice work completing all 40 questions." if warm
             else "You have completed all 40 questions.")
    strongest = performance["strongest_skill"]
    if strongest:
        second = (f"On {strongest['display_name']}, you answered "
                  f"{strongest['correct']} of {strongest['total']} "
                  "questions correctly.")
    else:
        second = "Your results by topic are shown below."
    growth = performance["growth_skill"]
    if growth:
        third = (f"For your next step, practice more questions on "
                 f"{growth['display_name']} ({growth['correct']} of "
                 f"{growth['total']} correct).")
    else:
        third = "Keep practicing the topics shown below."
    return f"{first} {second} {third}"
