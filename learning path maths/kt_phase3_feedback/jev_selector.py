"""Hosted Jev selector adapter for the private synthetic review seam.

This module implements the Selector protocol of ``synthetic_feedback``
against TypeSafe AI's Jev HTTP API. It is used ONLY for the synthetic,
private review path (``synthetic_feedback_demo.py --jev``); nothing here
runs at import time, and no live student flow touches it. The Aitta /
phrasing-generator side remains unimplemented.

Boundaries:

- Standard library only. The API key is validated, stored privately, and
  is never repr'd, logged, or placed in error text. All request and
  response failures surface as ``JevSelectionError('Jev request
  failed')`` with no raw body, credential, or HTTP detail attached.
- The default opener refuses redirects so the Authorization header can
  never be forwarded to a foreign host. No retries.
- Response bodies are capped at 1 MiB and must be UTF-8 JSON. Only the
  choice/type of the single answer, the response ``model`` string, and
  the token ``usage`` counts are parsed; extra fields (probabilities,
  confidence, other questions) are ignored and never consulted.
  Jev probabilities/confidence are not evidence that a selection is
  correct, so they are not kept in metadata.
- Selection payloads are revalidated locally before any request: the
  input contract, candidate set, and safe-evidence allowlist are shared
  with ``synthetic_feedback`` (single source of truth, imported below),
  so a malformed or widened payload cannot reach the wire. A midpoint
  payload has exactly one permitted candidate and is resolved locally
  without a request.
"""
import copy
import json
import math
import os
import urllib.error
import urllib.request
from pathlib import Path

from synthetic_feedback import (
    AUDIENCES, CHECKPOINTS, INPUT_SCHEMA, SELECTION_SCHEMA,
    _build_candidates, _safe_evidence, _validate_input)

MODEL = "jev-latest"
ENDPOINT = "https://api.typesafe.ai/v1/systemone"
QUESTION_ID = "feedback_candidate"
SELECTION_PROMPT_VERSION = "jev_selection_v1"
SELECTION_INSTRUCTIONS = (
    "Choose one permitted communication strategy for a private "
    "synthetic assessment-feedback review. Use only the observed "
    "evidence in state. Do not infer mastery, misconceptions, learning, "
    "fatigue, or prerequisites. Choose observed_summary when presenting "
    "the supplied end-of-assessment counts is appropriate; choose "
    "neutral when only a neutral completion acknowledgement is "
    "appropriate. These are unapproved draft strategies, not "
    "learner-ready advice."
)
CRITERIA = {
    "observed_summary": (
        "Describe only the supplied observed skill counts and total on "
        "this assessment. Counts will be rendered by application code; "
        "do not diagnose ability or recommend prerequisites."),
    "neutral": (
        "Offer only neutral encouragement or acknowledge completion, "
        "without citing performance counts or making ability claims."),
}

ENV_KEY = "TYPESAFE_API_KEY"
DEFAULT_ENV_PATH = Path(__file__).resolve().parent / ".env"
_ENV_ERROR = ("Set TYPESAFE_API_KEY in the environment or the local "
              "Phase 3 .env file")
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_MODEL_CHARS = 100
_SELECTION_KEYS = frozenset(
    ("schema", "audience", "checkpoint", "evidence", "candidates"))


class JevSelectionError(Exception):
    """Sanitized provider failure; carries no credential or raw detail."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so the Bearer token cannot leak to other hosts."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _default_opener():
    return urllib.request.build_opener(_NoRedirectHandler())


def _validate_api_key(api_key: str) -> str:
    if not isinstance(api_key, str):
        raise ValueError("TYPESAFE_API_KEY must be a nonempty ASCII token")
    key = api_key.strip()
    if (not key or key.casefold().startswith("paste-your")
            or any(ord(char) < 0x21 or ord(char) > 0x7E
                   for char in key)):
        raise ValueError("TYPESAFE_API_KEY must be a nonempty ASCII token")
    return key


def _validate_timeout(timeout) -> float:
    if (isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError(
            "timeout must be a positive finite number of seconds")
    return float(timeout)


def _read_env_file(path: Path) -> str:
    """Parse a single-key local .env; never prints lines or values."""
    try:
        text = path.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError):
        raise ValueError(_ENV_ERROR) from None
    value = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, raw = stripped.partition("=")
        if not sep or name.strip() != ENV_KEY or value is not None:
            raise ValueError(_ENV_ERROR) from None
        candidate = raw.strip()
        if (len(candidate) >= 2 and candidate[0] == candidate[-1]
                and candidate[0] in "\"'"):
            candidate = candidate[1:-1]
        value = candidate
    if not value:
        raise ValueError(_ENV_ERROR) from None
    return value


def _validated_selection_payload(payload: dict) -> dict:
    """Revalidate the exact selection payload locally before any wire.

    Returns freshly reconstructed evidence/candidates (the same objects
    the review contract allows) so nothing extra can reach ``state``.
    """
    if not isinstance(payload, dict) or set(payload) != _SELECTION_KEYS:
        raise ValueError(
            "selection payload must contain exactly schema, audience, "
            "checkpoint, evidence, candidates")
    if payload["schema"] != SELECTION_SCHEMA:
        raise ValueError(
            f"selection payload schema must be {SELECTION_SCHEMA!r}")
    audience = payload["audience"]
    checkpoint = payload["checkpoint"]
    if not isinstance(audience, str) or audience not in AUDIENCES:
        raise ValueError(
            "selection payload audience must be 'student' or 'teacher'")
    if not isinstance(checkpoint, str) or checkpoint not in CHECKPOINTS:
        raise ValueError(
            "selection payload checkpoint must be 'midpoint' or 'end'")
    if audience == "teacher" and checkpoint == "midpoint":
        raise ValueError("teacher midpoint feedback is not offered")
    if checkpoint == "midpoint":
        candidates = _build_candidates("midpoint", [])
        evidence = payload["evidence"]
        if not isinstance(evidence, dict) or evidence != {}:
            raise ValueError("midpoint selection evidence must be empty")
        if payload["candidates"] != candidates:
            raise ValueError(
                "midpoint selection candidates do not match policy")
        return {"audience": audience, "checkpoint": checkpoint,
                "evidence": {}, "candidates": candidates}
    evidence = payload["evidence"]
    if (not isinstance(evidence, dict)
            or set(evidence) != {"skills", "total"}):
        raise ValueError(
            "end selection evidence must contain exactly skills, total")
    normalized = _validate_input({
        "schema": INPUT_SCHEMA, "data_origin": "synthetic",
        "audience": audience, "checkpoint": checkpoint,
        "skills": evidence["skills"]})
    expected_evidence = _safe_evidence(checkpoint, normalized["skills"])
    expected_candidates = _build_candidates(
        checkpoint, normalized["skills"])
    if evidence["skills"] != expected_evidence["skills"]:
        raise ValueError("end selection skills do not match the contract")
    total = evidence["total"]
    if (not isinstance(total, dict)
            or set(total) != {"correct", "out_of"}
            or isinstance(total["correct"], bool)
            or not isinstance(total["correct"], int)
            or isinstance(total["out_of"], bool)
            or not isinstance(total["out_of"], int)
            or total != expected_evidence["total"]):
        raise ValueError(
            "end selection total must be the deterministic total")
    if payload["candidates"] != expected_candidates:
        raise ValueError("end selection candidates do not match policy")
    return {"audience": audience, "checkpoint": checkpoint,
            "evidence": expected_evidence,
            "candidates": expected_candidates}


def _parse_response(data, permitted_ids: set):
    """Parse only choice/type, model, and usage; ignore the rest."""
    answers = data.get("answers") if isinstance(data, dict) else None
    answer = answers.get(QUESTION_ID) if isinstance(answers, dict) else None
    if not isinstance(answer, dict) or answer.get("type") != "choice":
        raise JevSelectionError("Jev request failed") from None
    choice = answer.get("choice")
    if not isinstance(choice, str) or choice not in permitted_ids:
        raise JevSelectionError("Jev request failed") from None
    model = data.get("model")
    if (not isinstance(model, str) or not model.strip()
            or len(model) > _MAX_MODEL_CHARS):
        raise JevSelectionError("Jev request failed") from None
    usage = data.get("usage")
    if (not isinstance(usage, dict)
            or set(usage) != {"input_tokens", "output_tokens"}
            or any(isinstance(usage[key], bool)
                   or not isinstance(usage[key], int)
                   or usage[key] < 0
                   for key in ("input_tokens", "output_tokens"))):
        raise JevSelectionError("Jev request failed") from None
    return choice, model, {"input_tokens": usage["input_tokens"],
                           "output_tokens": usage["output_tokens"]}


class JevSelector:
    """Selector-protocol adapter for TypeSafe AI's hosted Jev API."""

    def __init__(self, api_key: str, timeout: float = 30.0,
                 opener=None):
        self._api_key = _validate_api_key(api_key)
        self._timeout = _validate_timeout(timeout)
        self._opener = opener if opener is not None else _default_opener()
        self._last_metadata = {"status": "not_called",
                               "model_version": None, "usage": None}

    def __repr__(self):
        return f"JevSelector(timeout={self._timeout!r})"

    @property
    def last_metadata(self) -> dict:
        return copy.deepcopy(self._last_metadata)

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> "JevSelector":
        """Build from TYPESAFE_API_KEY in os.environ or the local .env.

        An environment value always wins over the file; a set-but-invalid
        value raises instead of silently falling back. The file is the
        Phase 3-local ``.env`` only -- the repository root ``.env`` and
        any global files are never read, and nothing is loaded at import
        time.
        """
        if ENV_KEY in os.environ:
            return cls(os.environ[ENV_KEY])
        path = (Path(env_path) if env_path is not None
                else DEFAULT_ENV_PATH)
        return cls(_read_env_file(path))

    def select(self, payload: dict) -> dict:
        self._last_metadata = {"status": "not_called",
                               "model_version": None, "usage": None}
        normalized = _validated_selection_payload(payload)
        candidates = normalized["candidates"]
        if len(candidates) == 1:
            self._last_metadata = {
                "status": "not_called_single_candidate",
                "model_version": None, "usage": None}
            return {"candidate_id": candidates[0]["candidate_id"]}
        state = {"audience": normalized["audience"],
                 "checkpoint": normalized["checkpoint"],
                 "evidence": normalized["evidence"],
                 "candidates": normalized["candidates"]}
        body = {"model": MODEL, "state": state,
                "questions": {QUESTION_ID: {
                    "type": "choice",
                    "instructions": SELECTION_INSTRUCTIONS,
                    "criteria": {c["candidate_id"]:
                                 CRITERIA[c["candidate_id"]]
                                 for c in candidates}}}}
        permitted = {c["candidate_id"] for c in candidates}
        try:
            data = self._request(body)
            choice, model, usage = _parse_response(data, permitted)
        except JevSelectionError:
            self._last_metadata = {"status": "failed",
                                   "model_version": None, "usage": None}
            raise
        self._last_metadata = {"status": "completed",
                               "model_version": model, "usage": usage}
        return {"candidate_id": choice}

    def _request(self, body: dict):
        request = urllib.request.Request(
            ENDPOINT, data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._api_key}",
                     "Content-Type": "application/json",
                     "Accept": "application/json"},
            method="POST")
        try:
            response = self._opener.open(request, timeout=self._timeout)
            try:
                raw = response.read(_MAX_RESPONSE_BYTES + 1)
            finally:
                close = getattr(response, "close", None)
                if callable(close):
                    close()
        except JevSelectionError:
            raise
        except Exception:
            raise JevSelectionError("Jev request failed") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise JevSelectionError("Jev request failed") from None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise JevSelectionError("Jev request failed") from None
