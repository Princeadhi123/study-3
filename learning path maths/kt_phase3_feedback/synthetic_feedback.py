"""Provider-independent synthetic feedback review foundation (Phase 3).

This module is a local, review-only foundation for a future Jev-style
candidate selector plus a hosted phrasing generator. It is not an Aitta
or Jev adapter: provider identity, endpoints, and APIs are pending
provider documentation, so only an injection seam exists here.

Boundaries:

- Standard library only. No network, file, credential, or environment
  access, and no torch import. Nothing is persisted or logged by this
  module; the review result (including its trace) is returned to the
  caller only.
- ``data_origin="synthetic"`` is a caller attestation, not proof of
  origin. This module cannot verify how the counts were produced and
  never claims the evidence describes a real student.
- Outbound payloads are built fresh from a fixed allowlist. A student
  midpoint payload contains only audience, checkpoint, and permitted
  candidates: privately held midpoint skill rows are never sent out. An
  end payload carries only validated skill rows and a deterministic
  total. No identifiers beyond synthetic skill/candidate IDs are sent,
  and no raw question, answer, or session data leaves this module.
- Injected callbacks are arbitrary synchronous callables. They cannot be
  forcibly timed out from inside this module; selector/generator
  implementations are responsible for enforcing their own transport
  timeouts. Callbacks are invoked sequentially, each on a deep copy of
  a freshly built payload, and are never retried.
- Generated openings pass shape and heuristic checks only. That is not
  semantic grounding or an approval claim: every successful draft keeps
  ``requires_human_review=True``.
"""
import copy
import string
import time
from typing import Protocol

INPUT_SCHEMA = "phase3_synthetic_feedback_input_v1"
SELECTION_SCHEMA = "phase3_synthetic_selection_v1"
GENERATION_SCHEMA = "phase3_synthetic_generation_v1"
REVIEW_SCHEMA = "phase3_synthetic_feedback_review_v1"

CANDIDATE_VERSION = "synthetic_candidates_v1"
POLICY_VERSION = "synthetic_policy_v1"
PROMPT_VERSION = "synthetic_phrasing_v1"

GENERATION_INSTRUCTIONS = (
    "Write one short opening for a private synthetic feedback review. "
    "Return only a JSON object with candidate_id matching the supplied "
    "candidate and opening. Use no digits, numerical claims, topic names, "
    "diagnoses, mastery claims, prerequisite advice, claims of learning or "
    "fatigue, or advice about answers. For midpoint, offer only neutral "
    "encouragement to continue when ready. For end, briefly acknowledge "
    "completion of this assessment. Observed counts, if permitted by the "
    "selected candidate, are rendered separately by the application. Do "
    "not claim this draft is approved for learners."
)

AUDIENCES = ("student", "teacher")
CHECKPOINTS = ("midpoint", "end")
SKILL_COUNT = 4
MIDPOINT_OUT_OF = 5
END_OUT_OF = 10
END_TOTAL_OUT_OF = 40

REVIEW_STATUS = "draft_pending_educator_review"
NEUTRAL_CANDIDATE_ID = "neutral"
OBSERVED_CANDIDATE_ID = "observed_summary"

MIDPOINT_OPENING = ("You are halfway through the assessment. "
                    "Continue when you are ready.")
END_OPENING = "You have completed this assessment."
SCOPE_SUFFIX = ("These observations describe this assessment, "
                "not overall mastery.")

VALIDATION_GENERATED = "shape_and_heuristic_checks_only"
VALIDATION_DETERMINISTIC = "deterministic_fixed_text"

MAX_OPENING_CHARS = 300
MAX_SKILL_ID_CHARS = 40
MAX_SKILL_NAME_CHARS = 60

_SKILL_ID_CHARS = frozenset(string.ascii_letters + string.digits + "_-")
_SKILL_NAME_CHARS = frozenset(string.ascii_letters + " -")
_BANNED_TERMS = ("master", "misconception", "diagnos", "fatigue", "tired",
                 "prerequis", "learned", "improv", "percent", "score",
                 "correct", "incorrect")

_INPUT_KEYS = frozenset(
    ("schema", "data_origin", "audience", "checkpoint", "skills"))
_SKILL_KEYS = frozenset(("skill_id", "skill_name", "correct", "out_of"))


class Selector(Protocol):
    """Injected candidate selector, e.g. a future Jev adapter.

    Receives one deep-copied allowlisted payload and must return exactly
    ``{"candidate_id": "<one of the payload's candidate ids>"}``.
    Implementations own their own transport timeouts.
    """

    def select(self, payload: dict) -> dict:
        ...


class Generator(Protocol):
    """Injected phrasing generator, e.g. a future hosted LLM adapter.

    Receives one deep-copied allowlisted payload and must return exactly
    ``{"candidate_id": <the selected candidate id>, "opening": <str>}``.
    Implementations own their own transport timeouts.
    """

    def generate(self, payload: dict) -> dict:
        ...


def run_synthetic_feedback(evidence: dict, selector=None,
                           generator=None) -> dict:
    """Build a private review package for one synthetic evidence input.

    Raises ``ValueError`` on any invalid input before invoking a
    callback. Selector failures or invalid selections fall back to the
    rules baseline (first candidate) and skip the generator. Generator
    failures or invalid output fall back to deterministic wording for
    the same selected candidate; raw invalid output is never returned.
    """
    start = time.perf_counter()
    normalized = _validate_input(evidence)
    audience = normalized["audience"]
    checkpoint = normalized["checkpoint"]
    skills = normalized["skills"]
    candidates = _build_candidates(checkpoint, skills)
    safe_evidence = _safe_evidence(checkpoint, skills)

    selected = candidates[0]
    selection_source = "rules"
    fallback_reason = None
    selector_failed = False
    if selector is not None:
        payload = {"schema": SELECTION_SCHEMA,
                   "audience": audience,
                   "checkpoint": checkpoint,
                   "evidence": copy.deepcopy(safe_evidence),
                   "candidates": copy.deepcopy(candidates)}
        try:
            reply = selector.select(copy.deepcopy(payload))
        except Exception:
            fallback_reason = "selector_error"
            selector_failed = True
        else:
            chosen = _selection_reply(reply, candidates)
            if chosen is None:
                fallback_reason = "invalid_selection"
                selector_failed = True
            else:
                selected = chosen
                selection_source = "injected_selector"

    opening = MIDPOINT_OPENING if checkpoint == "midpoint" else END_OPENING
    phrasing_source = "deterministic"
    validation = VALIDATION_DETERMINISTIC
    if generator is not None and not selector_failed:
        payload = {"schema": GENERATION_SCHEMA,
                   "audience": audience,
                   "checkpoint": checkpoint,
                   "evidence": copy.deepcopy(safe_evidence),
                   "selected_candidate": copy.deepcopy(selected),
                   "prompt_version": PROMPT_VERSION,
                   "instructions": GENERATION_INSTRUCTIONS}
        try:
            reply = generator.generate(copy.deepcopy(payload))
        except Exception:
            fallback_reason = "generator_error"
        else:
            checked = _check_generated_opening(
                reply, selected["candidate_id"],
                [row["skill_name"] for row in skills])
            if checked is None:
                fallback_reason = "invalid_generation"
            else:
                opening = checked
                phrasing_source = "injected_generator"
                validation = VALIDATION_GENERATED

    template_baseline = _render(
        candidates[0],
        MIDPOINT_OPENING if checkpoint == "midpoint" else END_OPENING,
        checkpoint, skills)
    message = _render(selected, opening, checkpoint, skills)
    trace = {
        "candidate_version": CANDIDATE_VERSION,
        "policy_version": POLICY_VERSION,
        "prompt_version": PROMPT_VERSION,
        "selection_source": selection_source,
        "phrasing_source": phrasing_source,
        "validation": validation,
        "fallback_reason": fallback_reason,
        "latency_ms": round((time.perf_counter() - start) * 1000, 3),
        "provider_metadata": {
            "selector": (type(selector).__name__
                         if selector is not None else None),
            "generator": (type(generator).__name__
                          if generator is not None else None),
            "model_version": None,
            "cost": None,
        },
    }
    return {
        "schema": REVIEW_SCHEMA,
        "status": "draft_not_for_learner_delivery",
        "audience": audience,
        "checkpoint": checkpoint,
        "sanitized_evidence": copy.deepcopy(safe_evidence),
        "candidates": copy.deepcopy(candidates),
        "selected_candidate_id": selected["candidate_id"],
        "template_baseline": template_baseline,
        "message": message,
        "requires_human_review": True,
        "trace": trace,
    }


def _validate_input(evidence: dict) -> dict:
    """Validate the ``phase3_synthetic_feedback_input_v1`` contract."""
    if not isinstance(evidence, dict) or set(evidence) != _INPUT_KEYS:
        raise ValueError(
            "input must be a dict with exactly schema, data_origin, "
            "audience, checkpoint, skills")
    if evidence["schema"] != INPUT_SCHEMA:
        raise ValueError(f"schema must be {INPUT_SCHEMA!r}")
    if evidence["data_origin"] != "synthetic":
        raise ValueError(
            "data_origin must be the caller-attested value 'synthetic'")
    audience = evidence["audience"]
    checkpoint = evidence["checkpoint"]
    if not isinstance(audience, str) or audience not in AUDIENCES:
        raise ValueError("audience must be 'student' or 'teacher'")
    if not isinstance(checkpoint, str) or checkpoint not in CHECKPOINTS:
        raise ValueError("checkpoint must be 'midpoint' or 'end'")
    if audience == "teacher" and checkpoint == "midpoint":
        raise ValueError("teacher midpoint feedback is not offered")
    skills = evidence["skills"]
    if not isinstance(skills, list) or len(skills) != SKILL_COUNT:
        raise ValueError(
            f"skills must be a list of exactly {SKILL_COUNT} skill rows")
    expected_out_of = (MIDPOINT_OUT_OF if checkpoint == "midpoint"
                       else END_OUT_OF)
    seen = set()
    rows = []
    for index, row in enumerate(skills):
        if not isinstance(row, dict) or set(row) != _SKILL_KEYS:
            raise ValueError(
                f"skills[{index}] must be a dict with exactly "
                "skill_id, skill_name, correct, out_of")
        skill_id = row["skill_id"]
        name = row["skill_name"]
        correct = row["correct"]
        out_of = row["out_of"]
        if (not isinstance(skill_id, str) or not skill_id
                or len(skill_id) > MAX_SKILL_ID_CHARS
                or not set(skill_id) <= _SKILL_ID_CHARS):
            raise ValueError(
                f"skills[{index}].skill_id must be nonempty ASCII "
                f"alphanumeric/underscore/hyphen within "
                f"{MAX_SKILL_ID_CHARS} chars")
        if skill_id in seen:
            raise ValueError(f"skills[{index}] duplicates a skill_id")
        seen.add(skill_id)
        if (not isinstance(name, str) or not name.strip()
                or len(name) > MAX_SKILL_NAME_CHARS
                or not set(name) <= _SKILL_NAME_CHARS):
            raise ValueError(
                f"skills[{index}].skill_name must be nonempty ASCII "
                f"letters/spaces/hyphen within {MAX_SKILL_NAME_CHARS} "
                "chars")
        if (isinstance(correct, bool) or not isinstance(correct, int)
                or isinstance(out_of, bool)
                or not isinstance(out_of, int)):
            raise ValueError(
                f"skills[{index}] correct/out_of must be integers, "
                "not booleans")
        if out_of != expected_out_of or not 0 <= correct <= out_of:
            raise ValueError(
                f"skills[{index}] requires out_of {expected_out_of} at "
                f"{checkpoint} and 0 <= correct <= out_of")
        rows.append({"skill_id": skill_id, "skill_name": name,
                     "correct": correct, "out_of": out_of})
    return {"audience": audience, "checkpoint": checkpoint,
            "skills": rows}


def _build_candidates(checkpoint: str, skills: list[dict]) -> list[dict]:
    """Versioned candidate set; the first entry is the rules baseline."""
    neutral = {"candidate_id": NEUTRAL_CANDIDATE_ID,
               "strategy": "neutral_encouragement",
               "review_status": REVIEW_STATUS,
               "permitted_evidence": []}
    if checkpoint == "midpoint":
        return [neutral]
    observed = {"candidate_id": OBSERVED_CANDIDATE_ID,
                "strategy": "describe_observed_counts",
                "review_status": REVIEW_STATUS,
                "permitted_evidence":
                    [row["skill_id"] for row in skills] + ["total"]}
    return [observed, neutral]


def _safe_evidence(checkpoint: str, skills: list[dict]) -> dict:
    """Fresh allowlisted evidence; midpoint sends no skills or counts."""
    if checkpoint == "midpoint":
        return {}
    rows = [{"skill_id": row["skill_id"], "skill_name": row["skill_name"],
             "correct": row["correct"], "out_of": row["out_of"]}
            for row in skills]
    return {"skills": rows,
            "total": {"correct": sum(row["correct"] for row in rows),
                      "out_of": END_TOTAL_OUT_OF}}


def _selection_reply(reply: dict,
                     candidates: list[dict]) -> dict | None:
    """Accept exactly ``{"candidate_id": <known id>}``; else ``None``."""
    if (not isinstance(reply, dict) or set(reply) != {"candidate_id"}
            or not isinstance(reply["candidate_id"], str)):
        return None
    for candidate in candidates:
        if candidate["candidate_id"] == reply["candidate_id"]:
            return candidate
    return None


def _check_generated_opening(reply: dict, selected_id: str,
                             skill_names: list[str]) -> str | None:
    """Heuristic guard on generated openings; not semantic grounding.

    Returns the stripped opening when it passes shape and heuristic
    checks, else ``None``. Private midpoint skill names are checked too:
    the local validator sees names withheld from the outbound payload.
    """
    if (not isinstance(reply, dict)
            or set(reply) != {"candidate_id", "opening"}
            or reply["candidate_id"] != selected_id
            or not isinstance(reply["opening"], str)):
        return None
    opening = reply["opening"].strip()
    if not opening or len(opening) > MAX_OPENING_CHARS:
        return None
    if any(char.isnumeric() for char in opening):
        return None
    folded = opening.casefold()
    if any(term in folded for term in _BANNED_TERMS):
        return None
    if any(name.casefold() in folded for name in skill_names):
        return None
    return opening


def _render(candidate: dict, opening: str, checkpoint: str,
            skills: list[dict]) -> dict:
    """Render a candidate; observed count lines are application-built."""
    lines = []
    if candidate["candidate_id"] == OBSERVED_CANDIDATE_ID:
        lines = [f"{row['skill_name']}: {row['correct']} of "
                 f"{row['out_of']} correct on this assessment."
                 for row in skills]
        total = sum(row["correct"] for row in skills)
        lines.append(f"Observed total: {total} of {END_TOTAL_OUT_OF} "
                     "correct on this assessment.")
    return {"candidate_id": candidate["candidate_id"],
            "opening": opening,
            "evidence_lines": lines,
            "scope": SCOPE_SUFFIX,
            "text": " ".join([opening, *lines, SCOPE_SUFFIX])}
