"""Hosted Aitta phrasing adapter for the private synthetic review seam.

This module implements the Generator protocol of ``synthetic_feedback``
against the CSC OpenAI-compatible Aitta chat endpoint (the same style of
endpoint the existing ``tagging_common`` code uses; it is deliberately
not imported because that module has heavy dependencies and loads .env
files at import). This adapter is stdlib urllib only, is used ONLY for
the synthetic, private review path (``synthetic_feedback_demo.py
--aitta``), performs no I/O at import time, and is never touched by the
live student flow.

Boundaries:

- The API key and base URL are validated, stored privately, and never
  repr'd, logged, or echoed in errors. Every request/response/content
  failure surfaces as ``AittaGenerationError('Aitta request failed')``
  with no raw body, credential, or HTTP detail attached.
- Deliberately narrower wire than the local generation payload: the
  model receives ONLY ``{audience, checkpoint, selected_candidate:
  {candidate_id, strategy, review_status}}`` -- no evidence, counts,
  skill names, skill IDs, or permitted_evidence. The model writes a
  neutral opening; all facts are rendered by application code.
- Generation payloads are revalidated before any request by reusing the
  shared selection-payload validator from ``jev_selector`` and the
  input/candidate/opening policy from ``synthetic_feedback`` (single
  source of truth), so tampered or widened payloads cannot reach the
  wire. Rejected openings raise ``AittaGenerationError`` inside the
  adapter (runner sees ``generator_error``), unlike a returned-but-
  malformed reply which the runner would report as
  ``invalid_generation``.
- Same transport policy as the Jev adapter: default opener refuses
  redirects, zero retries, 1 MiB response cap, UTF-8 JSON only.
- Response parsing keeps only the JSON content object, the ``model``
  string, and token usage counts; finish reasons other than ``stop``,
  malformed content JSON, extra choices, and inconsistent usage are
  rejected. Raw model output and reasoning are never stored.
"""
import copy
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

from jev_selector import (
    _default_opener, _validate_api_key, _validate_timeout,
    _validated_selection_payload, SELECTION_SCHEMA)
from synthetic_feedback import (
    AUDIENCES, CHECKPOINTS, GENERATION_INSTRUCTIONS, GENERATION_SCHEMA,
    INPUT_SCHEMA, PROMPT_VERSION, _build_candidates,
    _check_generated_opening, _validate_input)

DEFAULT_MODEL = "openai/gpt-oss-120b"
MAX_TOKENS = 1024
REASONING_EFFORT = "low"
RESPONSE_FORMAT = {"type": "json_object"}

ENV_KEY = "AITTA_API_KEY"
ENV_BASE_URL = "AITTA_BASE_URL"
ENV_MODEL = "AITTA_MODEL"
_AITTA_ENV_KEYS = (ENV_KEY, ENV_BASE_URL, ENV_MODEL)
DEFAULT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_ENV_ERROR = ("Set AITTA_API_KEY and AITTA_BASE_URL in the environment "
              "or repository-root .env file")
_URL_ERROR = ("AITTA_BASE_URL must be an https base URL with no "
              "credentials, query, or fragment")
_MODEL_ERROR = "AITTA_MODEL must be a nonempty ASCII model name"
_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_MODEL_CHARS = 120
_GENERATION_KEYS = frozenset(
    ("schema", "audience", "checkpoint", "evidence",
     "selected_candidate", "prompt_version", "instructions"))


class AittaGenerationError(Exception):
    """Sanitized provider failure; carries no credential or raw detail."""


def _validate_base_url(base_url: str) -> str:
    """Validate the base URL and return the chat-completions endpoint.

    Any configured base path prefix is preserved (e.g. a reverse-proxy
    route prefix); the trailing slash is normalized and
    ``/chat/completions`` is appended exactly once. Malformed URLs and
    ports raise the static error without raw parse detail.
    """
    if not isinstance(base_url, str):
        raise ValueError(_URL_ERROR)
    url = base_url.strip()
    if not url or any(ord(char) <= 0x20 or ord(char) == 0x7F
                      for char in url):
        raise ValueError(_URL_ERROR)
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # validates port syntax/range if present
    except ValueError:
        raise ValueError(_URL_ERROR) from None
    if (parts.scheme != "https" or not parts.hostname
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment):
        raise ValueError(_URL_ERROR)
    path = parts.path.rstrip("/")
    return f"https://{parts.netloc}{path}/chat/completions"


def _validate_model(model: str) -> str:
    if (not isinstance(model, str) or not model
            or len(model) > _MAX_MODEL_CHARS
            or any(ord(char) < 0x21 or ord(char) > 0x7E
                   for char in model)):
        raise ValueError(_MODEL_ERROR)
    return model


def _read_root_env(path: Path) -> dict:
    """Read only AITTA_* keys from a root .env; never exposes contents.

    A missing file yields ``{}`` (env-only operation is allowed); parse
    problems on the selected keys raise the fixed generic error. All
    other assignments and non-assignment lines are ignored without
    parsing or logging their values.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError):
        raise ValueError(_ENV_ERROR) from None
    values = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, sep, raw = stripped.partition("=")
        name = name.strip()
        if not sep or name not in _AITTA_ENV_KEYS:
            continue
        if name in values:
            raise ValueError(_ENV_ERROR) from None
        value = raw.strip()
        if value and value[0] in "\"'":
            end = value.find(value[0], 1)
            if end == -1:
                raise ValueError(_ENV_ERROR) from None
            rest = value[end + 1:].strip()
            if rest and not rest.startswith("#"):
                raise ValueError(_ENV_ERROR) from None
            value = value[1:end]
        else:
            comment = value.find(" #")
            if comment != -1:
                value = value[:comment].rstrip()
        values[name] = value
    return values


def _validated_generation_payload(payload: dict) -> dict:
    """Revalidate the exact generation payload locally before any wire.

    The payload's evidence/candidates are checked against the shared
    selection contract by reconstructing a selection payload; the
    selected candidate must exactly equal one permitted object. Returns
    freshly reconstructed values only.
    """
    if not isinstance(payload, dict) or set(payload) != _GENERATION_KEYS:
        raise ValueError(
            "generation payload must contain exactly schema, audience, "
            "checkpoint, evidence, selected_candidate, prompt_version, "
            "instructions")
    if payload["schema"] != GENERATION_SCHEMA:
        raise ValueError(
            f"generation payload schema must be {GENERATION_SCHEMA!r}")
    if payload["prompt_version"] != PROMPT_VERSION:
        raise ValueError(
            f"generation payload prompt_version must be "
            f"{PROMPT_VERSION!r}")
    if payload["instructions"] != GENERATION_INSTRUCTIONS:
        raise ValueError(
            "generation payload instructions must match the fixed "
            "prompt")
    audience = payload["audience"]
    checkpoint = payload["checkpoint"]
    if not isinstance(audience, str) or audience not in AUDIENCES:
        raise ValueError(
            "generation payload audience must be 'student' or 'teacher'")
    if not isinstance(checkpoint, str) or checkpoint not in CHECKPOINTS:
        raise ValueError(
            "generation payload checkpoint must be 'midpoint' or 'end'")
    if checkpoint == "end":
        evidence = payload["evidence"]
        skills = (evidence.get("skills")
                  if isinstance(evidence, dict) else None)
        normalized_input = _validate_input({
            "schema": INPUT_SCHEMA, "data_origin": "synthetic",
            "audience": audience, "checkpoint": "end",
            "skills": skills})
        expected = _build_candidates("end", normalized_input["skills"])
    else:
        expected = _build_candidates("midpoint", [])
    normalized = _validated_selection_payload({
        "schema": SELECTION_SCHEMA, "audience": audience,
        "checkpoint": checkpoint, "evidence": payload["evidence"],
        "candidates": expected})
    selected = payload["selected_candidate"]
    matched = [c for c in normalized["candidates"] if c == selected]
    if not matched:
        raise ValueError(
            "generation payload selected_candidate must exactly equal "
            "a permitted candidate object")
    return {"audience": audience, "checkpoint": checkpoint,
            "evidence": normalized["evidence"],
            "candidates": normalized["candidates"],
            "selected_candidate": matched[0]}


class AittaGenerator:
    """Generator-protocol adapter for the OpenAI-compatible Aitta API."""

    def __init__(self, api_key: str, base_url: str,
                 model: str = DEFAULT_MODEL, timeout: float = 30.0,
                 opener=None):
        try:
            self._api_key = _validate_api_key(api_key)
        except ValueError:
            raise ValueError(_ENV_ERROR) from None
        self._endpoint = _validate_base_url(base_url)
        self._model = _validate_model(model)
        self._timeout = _validate_timeout(timeout)
        self._opener = opener if opener is not None else _default_opener()
        self._last_metadata = {"status": "not_called",
                               "model_version": None, "usage": None}

    def __repr__(self):
        return f"AittaGenerator(timeout={self._timeout!r})"

    @property
    def last_metadata(self) -> dict:
        return copy.deepcopy(self._last_metadata)

    @classmethod
    def from_env(cls, env_path: Path | None = None) -> "AittaGenerator":
        """Build from AITTA_* environment variables or the root .env.

        Environment values win individually; the repository-root ``.env``
        is read only when at least one of key/base-URL/model is unset
        (so the file can supply just the model). Only the three AITTA_*
        names are parsed -- other entries, including unrelated secrets,
        are ignored. A set-but-invalid environment value fails rather
        than falling back. The Phase 3-local ``.env`` is never read.
        """
        key = os.environ.get(ENV_KEY)
        base_url = os.environ.get(ENV_BASE_URL)
        model = os.environ.get(ENV_MODEL)
        if key is None or base_url is None or model is None:
            path = (Path(env_path) if env_path is not None
                    else DEFAULT_ENV_PATH)
            values = _read_root_env(path)
            if key is None:
                key = values.get(ENV_KEY)
            if base_url is None:
                base_url = values.get(ENV_BASE_URL)
            if model is None:
                model = values.get(ENV_MODEL)
        if not key or not base_url:
            raise ValueError(_ENV_ERROR) from None
        # A set-but-empty model must fail in cls; default only when unset.
        return cls(key, base_url,
                   model=model if model is not None else DEFAULT_MODEL)

    def generate(self, payload: dict) -> dict:
        self._last_metadata = {"status": "not_called",
                               "model_version": None, "usage": None}
        normalized = _validated_generation_payload(payload)
        selected = normalized["selected_candidate"]
        safe_context = {
            "audience": normalized["audience"],
            "checkpoint": normalized["checkpoint"],
            "selected_candidate": {
                "candidate_id": selected["candidate_id"],
                "strategy": selected["strategy"],
                "review_status": selected["review_status"]}}
        skill_names = [row["skill_name"] for row in
                       normalized["evidence"].get("skills", [])]
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": GENERATION_INSTRUCTIONS},
                {"role": "user", "content": json.dumps(safe_context)}],
            "max_tokens": MAX_TOKENS,
            "reasoning_effort": REASONING_EFFORT,
            "response_format": dict(RESPONSE_FORMAT)}
        try:
            data = self._request(body)
            opening, model, usage = self._parse(
                data, selected["candidate_id"], skill_names)
        except AittaGenerationError:
            self._last_metadata = {"status": "failed",
                                   "model_version": None, "usage": None}
            raise
        self._last_metadata = {"status": "completed",
                               "model_version": model, "usage": usage}
        return {"candidate_id": selected["candidate_id"],
                "opening": opening}

    def _request(self, body: dict):
        request = urllib.request.Request(
            self._endpoint, data=json.dumps(body).encode("utf-8"),
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
        except AittaGenerationError:
            raise
        except Exception:
            raise AittaGenerationError("Aitta request failed") from None
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise AittaGenerationError("Aitta request failed") from None
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise AittaGenerationError("Aitta request failed") from None

    def _parse(self, data, selected_id: str, skill_names: list):
        """Validate the OpenAI-shaped response; return sanitized parts."""
        choices = data.get("choices") if isinstance(data, dict) else None
        if not isinstance(choices, list) or len(choices) != 1:
            raise AittaGenerationError("Aitta request failed") from None
        choice = choices[0]
        if not isinstance(choice, dict):
            raise AittaGenerationError("Aitta request failed") from None
        message = choice.get("message")
        if (choice.get("finish_reason") != "stop"
                or not isinstance(message, dict)
                or not isinstance(message.get("content"), str)):
            raise AittaGenerationError("Aitta request failed") from None
        try:
            reply = json.loads(message["content"])
        except Exception:
            raise AittaGenerationError("Aitta request failed") from None
        opening = _check_generated_opening(reply, selected_id,
                                           skill_names)
        if opening is None or self._api_key in opening:
            raise AittaGenerationError("Aitta request failed") from None
        model = data.get("model")
        if (not isinstance(model, str) or not model
                or len(model) > _MAX_MODEL_CHARS
                or any(ord(char) < 0x21 or ord(char) > 0x7E
                       for char in model)
                or self._api_key in model):
            raise AittaGenerationError("Aitta request failed") from None
        usage = data.get("usage")
        required = ("prompt_tokens", "completion_tokens", "total_tokens")
        if (not isinstance(usage, dict)
                or any(key not in usage or isinstance(usage[key], bool)
                       or not isinstance(usage[key], int)
                       or usage[key] < 0 for key in required)
                or usage["prompt_tokens"] + usage["completion_tokens"]
                != usage["total_tokens"]):
            raise AittaGenerationError("Aitta request failed") from None
        return opening, model, {key: usage[key] for key in required}
