"""Hosted provider adapters for the observed-evidence feedback seam.

These adapters plug the real hosted providers (TypeSafe Jev selection,
Aitta phrasing) into the observed-evidence review contract defined by
``evidence_feedback`` and ``evidence_feedback_policy``. They are used
ONLY for the private synthetic review path; nothing runs at import time,
no live student flow touches them, and they add no new prompt text —
the only prompt content is the fixed ``SELECTION_INSTRUCTIONS``
and the existing ``GENERATION_INSTRUCTIONS`` constants.

Boundaries:

- ``EvidenceJevSelector`` subclasses ``JevSelector`` and reuses its
  validated credential handling, redirect-refusing opener, timeout,
  1 MiB response cap, and sanitized ``_request`` transport unchanged.
  The difference is the payload contract: the shared
  ``validate_selection_payload`` revalidates the exact observed-evidence
  selection payload before any request, so malformed or widened input
  cannot reach the wire. The wire state is only the freshly validated
  ``{audience, checkpoint, evidence, candidates}``; the single-choice
  question maps each candidate id to its fixed policy action (or
  strategy when the candidate carries no action). A single-candidate
  payload (midpoint neutral, or all-correct optional consolidation)
  resolves locally without a request.
- ``EvidenceAittaGenerator`` is a thin translation wrapper around an
  already-constructed ``AittaGenerator``. The hosted Aitta adapter's
  wire contract deliberately carries no evidence, so the observed-error
  counts cannot be forwarded to it: the wrapper accepts the narrow
  ``phase3_evidence_opening_v1`` payload, validates the selected
  candidate id/strategy/review-status against the fixed policy rules,
  and translates to the old ``phase3_synthetic_generation_v1`` payload
  using an all-zero local fixture skill table (safe placeholder names
  Arithmetic/Prices/Fractions/Percentages) purely to satisfy the old
  adapter's local payload validation. Those fabricated counts are
  validation scaffolding only: they never leave the process, the old
  wire sends only ``{audience, checkpoint, selected_candidate:
  {candidate_id, strategy, review_status}}``, and the wrapper reports
  no performance evidence anywhere. The returned opening keeps the
  original observed-evidence ``candidate_id`` so the runner's own
  opening check still applies against the real skill/subtopic names.
- All provider failures surface as the adapters' existing sanitized
  exceptions (``JevSelectionError``/``AittaGenerationError``) with no
  credential or raw detail; a response ``model`` string that contains
  the credential is rejected. ``last_metadata`` reports the fixed
  prompt versions and never claims provider confidence or rationale.
"""
import copy

import evidence_feedback
from aitta_generator import AittaGenerationError, AittaGenerator
from evidence_feedback_policy import (
    GENERATION_SCHEMA, REVIEW_STATUS, SELECTION_INSTRUCTIONS,
    SELECTION_PROMPT_VERSION)
from jev_selector import (
    MODEL, QUESTION_ID, JevSelectionError, JevSelector, _parse_response)
from synthetic_feedback import (
    GENERATION_INSTRUCTIONS, GENERATION_SCHEMA as SYNTHETIC_GENERATION_SCHEMA,
    PROMPT_VERSION, _build_candidates, _safe_evidence)

GENERATOR_ROLE = "opening_only_no_performance_evidence"

_STRATEGIES = frozenset(
    ("neutral_encouragement", "optional_consolidation",
     "focused_review", "supported_review"))
_OPENING_KEYS = frozenset(
    ("schema", "audience", "checkpoint", "selected_candidate"))
_CANDIDATE_KEYS = frozenset(
    ("candidate_id", "strategy", "review_status"))

# Local validation scaffolding for the old generation-payload contract.
# These rows are never sent anywhere: the Aitta wire carries only
# audience, checkpoint, and the selected candidate's id/strategy/status.
_FIXTURE_SKILLS = [
    {"skill_id": "skill_a", "skill_name": "Arithmetic",
     "correct": 0, "out_of": 10},
    {"skill_id": "skill_b", "skill_name": "Prices",
     "correct": 0, "out_of": 10},
    {"skill_id": "skill_c", "skill_name": "Fractions",
     "correct": 0, "out_of": 10},
    {"skill_id": "skill_d", "skill_name": "Percentages",
     "correct": 0, "out_of": 10},
]


def _selector_metadata(status, model=None, usage=None):
    return {"status": status, "model_version": model, "usage": usage,
            "prompt_version": SELECTION_PROMPT_VERSION}


def _generator_metadata(status, model=None, usage=None):
    return {"status": status, "model_version": model, "usage": usage,
            "prompt_version": PROMPT_VERSION, "role": GENERATOR_ROLE}


def _validated_opening_payload(payload: dict) -> dict:
    """Revalidate the narrow ``phase3_evidence_opening_v1`` payload.

    The payload carries no evidence at all, so validation is limited to
    the fixed envelope and the policy rules a selected candidate id and
    strategy must satisfy. Returns freshly reconstructed values only.
    """
    if not isinstance(payload, dict) or set(payload) != _OPENING_KEYS:
        raise ValueError(
            "generation payload must contain exactly schema, audience, "
            "checkpoint, selected_candidate")
    if payload["schema"] != GENERATION_SCHEMA:
        raise ValueError(
            f"generation payload schema must be {GENERATION_SCHEMA!r}")
    audience = payload["audience"]
    checkpoint = payload["checkpoint"]
    if (not isinstance(audience, str)
            or audience not in evidence_feedback.AUDIENCES):
        raise ValueError(
            "generation payload audience must be 'student' or 'teacher'")
    if (not isinstance(checkpoint, str)
            or checkpoint not in evidence_feedback.CHECKPOINTS):
        raise ValueError(
            "generation payload checkpoint must be 'midpoint' or 'end'")
    if audience == "teacher" and checkpoint == "midpoint":
        raise ValueError("teacher midpoint feedback is not offered")
    selected = payload["selected_candidate"]
    if not isinstance(selected, dict) or set(selected) != _CANDIDATE_KEYS:
        raise ValueError(
            "selected_candidate must contain exactly candidate_id, "
            "strategy, review_status")
    candidate_id = selected["candidate_id"]
    if not isinstance(candidate_id, str) or not candidate_id:
        raise ValueError("selected_candidate candidate_id is invalid")
    strategy = selected["strategy"]
    if not isinstance(strategy, str) or strategy not in _STRATEGIES:
        raise ValueError("selected_candidate strategy is not permitted")
    if selected["review_status"] != REVIEW_STATUS:
        raise ValueError(
            "selected_candidate review_status must match the policy")
    if checkpoint == "midpoint":
        if candidate_id != "neutral" or strategy != "neutral_encouragement":
            raise ValueError(
                "midpoint permits only the neutral encouragement "
                "candidate")
    elif candidate_id == "neutral":
        raise ValueError("the neutral candidate is midpoint-only")
    elif candidate_id == "optional_consolidation":
        if strategy != "optional_consolidation":
            raise ValueError(
                "optional_consolidation requires its own strategy")
    elif (candidate_id.startswith("review_")
          and evidence_feedback.ID_PATTERN.fullmatch(candidate_id[7:])):
        if strategy not in ("focused_review", "supported_review"):
            raise ValueError(
                "review candidates require a focused or supported "
                "review strategy")
    else:
        raise ValueError("unknown selected_candidate id")
    return {"audience": audience, "checkpoint": checkpoint,
            "selected_candidate": dict(selected)}


class EvidenceJevSelector(JevSelector):
    """Jev selector for the observed-evidence candidate contract."""

    def __repr__(self):
        return f"EvidenceJevSelector(timeout={self._timeout!r})"

    def select(self, payload: dict) -> dict:
        self._last_metadata = _selector_metadata("not_called")
        normalized = evidence_feedback.validate_selection_payload(payload)
        candidates = normalized["candidates"]
        if len(candidates) == 1:
            self._last_metadata = _selector_metadata(
                "not_called_single_candidate")
            return {"candidate_id": candidates[0]["candidate_id"]}
        state = {"audience": normalized["audience"],
                 "checkpoint": normalized["checkpoint"],
                 "evidence": normalized["evidence"],
                 "candidates": normalized["candidates"]}
        body = {"model": MODEL, "state": state,
                "questions": {QUESTION_ID: {
                    "type": "choice",
                    "instructions": SELECTION_INSTRUCTIONS,
                    "criteria": {
                        c["candidate_id"]: (c["action"]
                                            if c["action"] is not None
                                            else c["strategy"])
                        for c in candidates}}}}
        permitted = {c["candidate_id"] for c in candidates}
        try:
            data = self._request(body)
            choice, model, usage = _parse_response(data, permitted)
            if self._api_key in model:
                raise JevSelectionError("Jev request failed")
        except JevSelectionError:
            self._last_metadata = _selector_metadata("failed")
            raise
        self._last_metadata = _selector_metadata("completed", model, usage)
        return {"candidate_id": choice}


class EvidenceAittaGenerator:
    """Opening-only Aitta wrapper for the observed-evidence contract.

    Translates the narrow ``phase3_evidence_opening_v1`` payload into
    the existing ``AittaGenerator`` contract. The hosted wire already
    excludes evidence, so the observed counts are deliberately not
    forwarded: a fixed all-zero fixture table exists only to satisfy the
    old adapter's local payload validation and never leaves this
    process. The reply is returned under the original candidate id so
    the runner's opening checks still apply to real topic names.
    """

    def __init__(self, generator: AittaGenerator):
        if not isinstance(generator, AittaGenerator):
            raise TypeError(
                "EvidenceAittaGenerator wraps an AittaGenerator instance")
        self._generator = generator
        self._last_metadata = _generator_metadata("not_called")

    def __repr__(self):
        return f"EvidenceAittaGenerator({self._generator!r})"

    @property
    def last_metadata(self) -> dict:
        return copy.deepcopy(self._last_metadata)

    def generate(self, payload: dict) -> dict:
        self._last_metadata = _generator_metadata("not_called")
        normalized = _validated_opening_payload(payload)
        checkpoint = normalized["checkpoint"]
        if checkpoint == "midpoint":
            evidence = {}
            candidates = _build_candidates("midpoint", [])
        else:
            evidence = _safe_evidence("end", _FIXTURE_SKILLS)
            candidates = _build_candidates("end", _FIXTURE_SKILLS)
        legacy = {
            "schema": SYNTHETIC_GENERATION_SCHEMA,
            "audience": normalized["audience"],
            "checkpoint": checkpoint,
            "evidence": evidence,
            "selected_candidate": candidates[0],
            "prompt_version": PROMPT_VERSION,
            "instructions": GENERATION_INSTRUCTIONS}
        try:
            reply = self._generator.generate(legacy)
        except AittaGenerationError:
            self._last_metadata = _generator_metadata("failed")
            raise
        self._last_metadata = {**self._generator.last_metadata,
                               "prompt_version": PROMPT_VERSION,
                               "role": GENERATOR_ROLE}
        return {"candidate_id": normalized["selected_candidate"]
                ["candidate_id"], "opening": reply["opening"]}
