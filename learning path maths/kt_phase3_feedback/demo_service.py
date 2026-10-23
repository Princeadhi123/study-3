"""Local synthetic demo service: sessions, jobs, diagnostics, reviews.

This is a localhost research demo over the approved private bank. It is
not the live student API: every session is attested synthetic, all
feedback remains a draft requiring educator review, and KT
values stay private research diagnostics that never reach student
payloads.

Provider calls are optional and bounded; transient transport failures
retry a bounded number of times, and a failed replay case cancels all
future provider work for that session.
The single provider worker owns the Cached* adapters so provider state
is shared by exactly one thread; the diagnostics worker owns the frozen
model. Neither pool blocks student submissions.
"""
import contextlib
import copy
import hashlib
import hmac
import json
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import educator_review_contract
import phase3_paths
import worked_review_math
from aitta_generator import AittaGenerator
from assessment_worked_review import (
    ASSESSMENT_WORKED_REVIEW_VERSION, GROUNDED_TEMPLATE_VERSION,
    QUESTION_FIRST_VERSION, concise_student_preview,
    question_first_preview)
from active_payload import active_payload
from evidence_feedback import (
    MIDPOINT_MESSAGE_VERSION, _overlay_worked_review, build_evidence,
    build_research_evidence, bundle_selection_payload, canonical_digest,
    compose_midpoint_message, run_feedback, run_multi_feedback,
    select_review_bundle, validate_evidence)
from evidence_feedback_policy import (
    MULTI_REVIEW_POLICY_VERSION, MULTI_SELECTION_INSTRUCTIONS,
    MULTI_SELECTION_PROMPT_VERSION,
    POLICY_VERSION, SELECTION_INSTRUCTIONS, SELECTION_PROMPT_VERSION)
from feedback_practice import (
    FEEDBACK_BUNDLE_VERSION, INTEGRATED_TEMPLATE_VERSION,
    MULTI_INTEGRATED_TEMPLATE_VERSION, PRACTICE_STATUS_MESSAGES,
    empty_context, project_practice_context, resolve_practice,
    review_example, validate_practice_context)
from feedback_service import assessment_feedback_graph
from full_feedback import (
    CachedFullFeedbackGenerator, FullFeedbackAittaGenerator,
    FULL_FEEDBACK_INSTRUCTIONS, FULL_FEEDBACK_PROMPT_VERSION,
    FULL_FEEDBACK_SECTION_KEYS, FULL_FEEDBACK_SECTION_TITLES,
    MULTI_FULL_FEEDBACK_INSTRUCTIONS,
    MULTI_FULL_FEEDBACK_PROMPT_VERSION,
    structured_sections)
from live_diagnostics import LiveDiagnostics
from mcq_service import HALF_LENGTH, FULL_LENGTH, MCQSessionService
from midpoint_feedback import (
    MESSAGE_VERSION as MIDPOINT_DRAFT_VERSION,
    PROMPT_VERSION as MIDPOINT_PROMPT_VERSION,
    CachedMidpointGenerator, MidpointAittaGenerator,
    build_midpoint_input, compose_midpoint_draft)
from replay_provider_scenarios import CaptureCache
from research_workspace import DEFAULT_REPORT, ResearchWorkspace, case_display
from research_runtime import (
    BANK_LABELS, load_research_bank, require_augmented_embeddings,
    teacher_content_context, validate_current_practice_pool)
from scenario_replays import DEFAULT_SOURCE, ScenarioReplays
from scenario_runner import generate_responses
from schemas import SESSION_SCHEMA
from session_store import SessionStore, bank_fingerprint, utc_now
from shadow_practice import (
    ShadowPracticeRecommender, validate_practice_pool, validate_target_band)
from synthetic_feedback import PROMPT_VERSION
import worked_example_selection
from worked_example_generation import (
    CachedWorkedExampleGenerator, WorkedExampleAittaGenerator,
    build_input as build_worked_example_input,
    validate_reply as validate_worked_example_reply)
from worked_first_context import (
    CONTEXT_VERSION as WORKED_FIRST_CONTEXT_VERSION,
    PROMPT_VERSION as WORKED_FIRST_GENERATION_PROMPT_VERSION,
    build_worked_first_context)

DEFAULT_ROOT = phase3_paths.ARTIFACTS / "live_demo_runtime"
SESSION_ID_RE = re.compile(r"\A[a-f0-9]{32}\Z")
META_SCHEMA = "phase3_live_demo_session_meta_v1"
CHECKPOINT_LENGTHS = {"midpoint": HALF_LENGTH, "end": FULL_LENGTH}
SELECTOR_FAILURE_REASONS = frozenset({
    "selector_error", "mutated_selection_payload", "invalid_selection"})
SIMULATION_PROFILES = (
    "all_correct", "all_incorrect", "weak_fractions_only",
    "first_half_correct_second_half_wrong", "alternating")
PUBLIC_QUESTION_KEYS = (
    "skill_id", "text", "options")
TEMPLATE_VERSION = "phase3_student_feedback_template_v1"
FEEDBACK_VARIANTS = ("question_first", "worked_first")
WORKED_FIRST_VERSION = "worked_first_full_feedback_v1"
WORKED_FIRST_TEMPLATE_VERSION = (
    "phase3_student_feedback_worked_first_full_v1")

EDIT_CONTENT_KEYS = {"section_titles", "skill_labels",
                     "review_examples", "practice_activities",
                     "worked_review_notice"}
EDIT_REVIEW_FIELDS = {"title", "for_subtopic_name", "prompt",
                      "options", "selected_answer", "correct_answer",
                      "steps", "explanation", "reasoning_question",
                      "notice", "socratic_prompt", "option_contrast",
                      "progressive_hints"}
EDIT_PRACTICE_FIELDS = {"topic_label", "message", "notice",
                        "instruction", "question", "relationship"}
EDIT_QUESTION_FIELDS = {"skill_name", "concept_name", "text",
                        "options"}
EDIT_MAX_LIST = 30
EDIT_MAX_TEXT = 2000


class ConflictError(Exception):
    """409: submitted against a changed or missing resource revision."""

    def __init__(self, message, current=None):
        super().__init__(message)
        self.current = current


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _strict_text(value, field, maximum):
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if any(ord(ch) < 0x20 and ch not in "\n\t" for ch in value):
        raise ValueError(f"{field} must not contain control characters")
    if len(value) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return value


_DOCUMENT_BLOCK_FIELDS = {
    "heading": {"kind", "text"},
    "paragraph": {"kind", "text"},
    "unordered_list": {"kind", "items"},
    "ordered_list": {"kind", "items"}}


def _document_blocks_text(blocks):
    parts = []
    for block in blocks:
        if block["kind"] == "unordered_list":
            parts.append("\n".join(f"- {item}" for item in
                                   block["items"]))
        elif block["kind"] == "ordered_list":
            parts.append("\n".join(f"{i}. {item}" for i, item in
                                   enumerate(block["items"], 1)))
        else:
            parts.append(block["text"])
    return "\n\n".join(parts)


def _validated_document_blocks(value):
    if not isinstance(value, list) or not value:
        raise ValueError("document_blocks must be a nonempty list")
    if len(value) > 500:
        raise ValueError("document_blocks exceeds 500 blocks")
    blocks = []
    for index, block in enumerate(value):
        field = f"document_blocks[{index}]"
        kind = block.get("kind") if isinstance(block, dict) else None
        if not isinstance(kind, str) or kind not in _DOCUMENT_BLOCK_FIELDS:
            raise ValueError(f"{field}.kind is not a supported block kind")
        if set(block) != _DOCUMENT_BLOCK_FIELDS[kind]:
            raise ValueError(f"{field} has unexpected fields")
        if kind in ("heading", "paragraph"):
            text = _strict_text(block["text"], f"{field}.text", 32768)
            if kind == "heading" and not text.strip():
                raise ValueError(f"{field}.text must be nonempty")
            blocks.append({"kind": kind, "text": text})
        else:
            items = block["items"]
            if (not isinstance(items, list) or not items
                    or len(items) > 100):
                raise ValueError(
                    f"{field}.items must be 1-100 strings")
            cleaned = []
            for item_index, item in enumerate(items):
                text = _strict_text(
                    item, f"{field}.items[{item_index}]", 32768)
                if not text.strip():
                    raise ValueError(
                        f"{field}.items[{item_index}] must be nonempty")
                cleaned.append(text)
            blocks.append({"kind": kind, "items": cleaned})
    if len(_document_blocks_text(blocks)) > 32768:
        raise ValueError("document_blocks exceeds 32768 serialized "
                         "characters")
    return blocks


class DemoService:
    """Owns demo sessions, async provider/diagnostics jobs, and reviews."""

    def __init__(self, root=DEFAULT_ROOT, bank=None, taxonomy=None,
                 provider_mode="rules", jev_env_file=None, call_budget=12,
                 provider_timeout=120.0, diagnostics=None,
                 selector=None, generator=None, max_pending_jobs=64,
                 replay_report=DEFAULT_REPORT, replay_source=DEFAULT_SOURCE,
                 practice_pool=None, practice_target_band=None,
                 recommender=None, replay_bank_mode="demo",
                 review_mode="aligned_two_topic",
                 provider_workers=8, diagnostics_workers=4,
                 midpoint_generator=None, worked_example_generator=None):
        # Validate opt-in shadow wiring before any directory/pool/provider
        # side effects.
        if (practice_pool is None) != (practice_target_band is None):
            raise ValueError(
                "shadow practice requires pool and target band together")
        if recommender is not None and (
                practice_pool is not None or practice_target_band is not None):
            raise ValueError(
                "injected recommender cannot combine with practice pool/band")
        if practice_pool is not None:
            validate_practice_pool(practice_pool)
            validate_current_practice_pool(practice_pool)
            require_augmented_embeddings()
            validate_target_band(practice_target_band)
        if recommender is not None and hasattr(recommender, "pool"):
            validate_current_practice_pool(recommender.pool)
            require_augmented_embeddings()
        if (not isinstance(replay_bank_mode, str)
                or replay_bank_mode not in BANK_LABELS):
            raise ValueError("unknown replay bank mode")
        if review_mode not in ("aligned_two_topic", "legacy_single"):
            raise ValueError("unknown review mode")
        self.review_mode = review_mode
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._closing = False
        self.research = ResearchWorkspace(self.root, replay_report)
        default_bank = bank is None
        if default_bank:
            bank = json.loads(phase3_paths.require(
                phase3_paths.APPROVED_BANK).read_text(encoding="utf-8"))
        if taxonomy is None:
            taxonomy = json.loads(phase3_paths.require(
                phase3_paths.ASSESSMENT_TAXONOMY).read_text(encoding="utf-8"))
        self.bank, self.taxonomy = bank, taxonomy
        self.store = SessionStore(self.root / "sessions")
        self.mcqs = MCQSessionService(bank, self.store, taxonomy=taxonomy)
        self._banks = {"demo": {
            "bank": bank, "taxonomy": taxonomy, "mcqs": self.mcqs,
            "provenance": {
                "bank_mode": "demo", "bank_identity": "demo_bank",
                "bank_label": BANK_LABELS["demo"], "session_mode": "synthetic_demo",
                "validation_mode": "approved_demo_bank",
                "bank_sha256": bank_fingerprint(bank),
                "source_path": str(phase3_paths.APPROVED_BANK) if default_bank else "injected",
                "source_sha256": hashlib.sha256(phase3_paths.APPROVED_BANK.read_bytes()).hexdigest()
                if default_bank else None,
                "declared_item_regime": "mixed_frozen_vocabulary",
            }}}
        self.replay_bank_mode = replay_bank_mode
        if replay_bank_mode != "demo":
            self._bank_context(replay_bank_mode)
        self.meta_dir = self.root / "metadata"
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self._provider_call_lock = threading.RLock()
        self.provider_pool = ThreadPoolExecutor(
            max_workers=provider_workers, thread_name_prefix="demo-provider")
        self.diag_pool = ThreadPoolExecutor(
            max_workers=diagnostics_workers,
            thread_name_prefix="demo-diagnostics")
        self.max_pending_jobs = max_pending_jobs
        self._pending_provider = 0
        self._pending_diag = 0
        # Per-session cancellation signals for replay background work;
        # set when a replay case fails so late midpoint/end provider
        # jobs stop retrying and never publish.
        self._provider_session_stops = {}
        self.diagnostics = (diagnostics if diagnostics is not None
                            else LiveDiagnostics())
        if provider_mode not in ("rules", "hosted"):
            raise ValueError("provider_mode must be rules or hosted")
        self.provider_mode = provider_mode
        self.call_budget = call_budget
        self.provider_timeout = provider_timeout
        self.cache = None
        self.midpoint_generator = midpoint_generator
        # Injected scoped worked-example adapter (tests or pre-wired
        # hosted providers); it never gains env access implicitly.
        self.worked_example_generator = worked_example_generator
        if selector is not None or generator is not None:
            # Injected adapters (tests or pre-wired hosted providers);
            # they never gain a hosted midpoint call implicitly.
            self.selector, self.generator = selector, generator
        elif provider_mode == "hosted":
            self.selector, self.generator = self._hosted_adapters(
                jev_env_file, call_budget, provider_timeout)
        else:
            self.selector, self.generator = None, None
        if midpoint_generator is not None:
            self.midpoint_generator = midpoint_generator
        if practice_pool is not None:
            self.recommender = ShadowPracticeRecommender(
                practice_pool, practice_target_band,
                self.diagnostics.predict_future_candidates)
        else:
            self.recommender = recommender
        self._recover_interrupted()
        self.replays = ScenarioReplays(self, replay_source)
        # Lazy frozen-model prewarm; serialized on the diagnostics worker.
        self.diag_pool.submit(self.diagnostics.warmup)

    # ---------------- lifecycle ----------------

    def _bank_context(self, bank_mode="demo"):
        if not isinstance(bank_mode, str) or bank_mode not in BANK_LABELS:
            raise ValueError("unknown bank mode")
        with self.lock:
            if bank_mode not in self._banks:
                bank, taxonomy, provenance = load_research_bank(bank_mode)
                self._banks[bank_mode] = {
                    "bank": bank, "taxonomy": taxonomy,
                    "mcqs": MCQSessionService(
                        bank, self.store, taxonomy=taxonomy, research_mode=True),
                    "provenance": provenance}
            return self._banks[bank_mode]

    def _context_for(self, meta):
        return self._bank_context(meta.get("bank_mode", "demo"))

    def _evidence(self, meta, rows):
        context = self._context_for(meta)
        builder = (build_evidence if meta.get("bank_mode", "demo") == "demo"
                   else build_research_evidence)
        return builder(context["bank"], context["taxonomy"], rows)

    def _assessment_context(self, meta, rows=None):
        if (meta.get("worked_review_version")
                != ASSESSMENT_WORKED_REVIEW_VERSION):
            return None
        if rows is None:
            rows = self._rows(meta["session_id"])
        if len(rows) != FULL_LENGTH:
            raise ValueError(
                "grounded worked review requires 40 recorded answers")
        context = self._context_for(meta)
        result = {"bank": context["bank"],
                  "taxonomy": context["taxonomy"],
                  "responses": rows}
        snapshot = meta["checkpoints"]["end"].get(
            worked_example_selection.CONTEXT_KEY)
        if snapshot is not None:
            result[worked_example_selection.CONTEXT_KEY] = (
                copy.deepcopy(snapshot))
        return result

    def _freeze_worked_example_selection(self, meta, rows=None):
        """Freeze the end-checkpoint worked-example selection snapshot
        once, before any draft is built; never overwrite an existing
        frozen snapshot. Callers hold the service lock and persist."""
        if (meta.get("worked_example_selection_policy")
                != worked_example_selection.POLICY):
            return
        end = meta["checkpoints"]["end"]
        if worked_example_selection.CONTEXT_KEY in end:
            return
        if rows is None:
            rows = self._rows(meta["session_id"])
        context = self._context_for(meta)
        end[worked_example_selection.CONTEXT_KEY] = (
            worked_example_selection.freeze_selection(
                context["bank"], rows, end.get("diagnostics")))

    @staticmethod
    def _is_grounded(meta):
        return (meta.get("worked_review_version")
                == ASSESSMENT_WORKED_REVIEW_VERSION)

    @staticmethod
    def _grounded_unavailable(review):
        from full_feedback import guarded_multi_message
        safe = review["sanitized_evidence"]

        def det(cands):
            return guarded_multi_message(safe, cands, review_examples=[])

        review["message"] = det(review["selected_candidates"])
        review["trace"]["guardrails_version"] = (
            review["message"]["guardrails_version"])
        review["trace"]["section_sources"] = copy.deepcopy(
            review["message"]["section_sources"])
        baseline = next(
            b for b in review["candidates"]
            if b["candidate_id"] == review["baseline_candidate_id"])
        review["template_baseline"] = det(baseline["review_candidates"])
        review["review_examples"] = []
        review["worked_review_version"] = ASSESSMENT_WORKED_REVIEW_VERSION
        review["worked_review_status"] = "unavailable"
        review["worked_review_notice"] = (
            "Assessment-answer grounding is unavailable. Ask your "
            "teacher to review the assessment answers; no worked "
            "example has been substituted.")
        review["trace"]["worked_review_context_status"] = "unavailable"
        review["trace"]["phrasing_source"] = "deterministic"
        review["trace"]["validation"] = (
            "deterministic_evidence_and_draft_actions")
        if not review["trace"].get("fallback_reason"):
            review["trace"]["fallback_reason"] = (
                "assessment_grounding_unavailable")
        return review

    @staticmethod
    def available_banks():
        return [{"bank_mode": mode, "label": label, "question_count": FULL_LENGTH,
                 "research_pending_review": mode != "demo"}
                for mode, label in BANK_LABELS.items()]

    def _hosted_adapters(self, jev_env_file=None, call_budget=None,
                         timeout=None):
        """Construct the hosted narrative provider once for the worker.

        Review-focus selection is deterministic rules for every demo
        mode; hosted wiring covers only the Aitta draft generator. The
        deprecated ``jev_env_file`` argument is accepted for backward
        compatibility and ignored.
        """
        inner = AittaGenerator.from_env()
        inner._timeout = timeout
        # One credential read; the shallow copy gives the midpoint
        # adapter its own opener slot so each observer stays separate.
        midpoint_inner = copy.copy(inner)
        midpoint_inner._meta_local = threading.local()
        midpoint_inner._fallback_metadata = dict(inner._fallback_metadata)
        worked_inner = copy.copy(inner)
        worked_inner._meta_local = threading.local()
        worked_inner._fallback_metadata = dict(inner._fallback_metadata)
        native_generator = FullFeedbackAittaGenerator(inner)
        captures = self.root / "provider_captures"
        captures.mkdir(exist_ok=True)
        self.cache = CaptureCache(captures, call_budget,
                                  automatic_retries=True,
                                  reuse_captures=False)
        endpoint_hash = hashlib.sha256(
            inner._endpoint.encode("utf-8")).hexdigest()
        self.midpoint_generator = CachedMidpointGenerator(
            MidpointAittaGenerator(midpoint_inner),
            self.cache, inner._model, endpoint_hash)
        if self.worked_example_generator is None:
            self.worked_example_generator = CachedWorkedExampleGenerator(
                WorkedExampleAittaGenerator(worked_inner),
                self.cache, inner._model, endpoint_hash)
        return (None, CachedFullFeedbackGenerator(
            native_generator, self.cache, inner._model, endpoint_hash))

    def _recover_interrupted(self):
        """Mark jobs orphaned by a prior process; never rerun providers."""
        sessions_dir = self.root / "sessions"
        if not sessions_dir.is_dir():
            return
        for path in sorted(sessions_dir.glob("*.json")):
            meta = self._try_load_meta(path.stem)
            if meta is None:
                continue
            with self.lock:
                changed = False
                for name, checkpoint in meta["checkpoints"].items():
                    changed = (self._recover_checkpoint_jobs(
                        meta, name, checkpoint) or changed)
                changed = (self._recover_provider_job(meta)
                           or changed)
                if changed:
                    self._save_meta(meta)

    def _recover_checkpoint_jobs(self, meta, name, checkpoint):
        """Recover every orphaned job inside one checkpoint. Returns
        whether the checkpoint was modified. Callers hold the lock;
        every pending job must be processed even when an earlier one
        already marked the checkpoint changed."""
        changed = False
        job = checkpoint.get("diagnostics_job")
        if job and job["status"] == "pending":
            job["status"] = "unavailable"
            job["reason"] = "interrupted_by_restart"
            checkpoint["diagnostics"] = {
                "status": "unavailable",
                "reason": "interrupted_by_restart"}
            changed = True
        for key in ("phrasing_job", "worked_example_job"):
            job = checkpoint.get(key)
            if job and job["status"] == "pending":
                job["status"] = "fallback"
                job["reason"] = "interrupted_by_restart"
                job["finished_at"] = utc_now()
                changed = True
        changed = (self._recover_recommendation(meta, name, checkpoint)
                   or changed)
        changed = (self._recover_diagnostics_alignment(name, checkpoint)
                   or changed)
        return changed

    def _recover_recommendation(self, meta, name, checkpoint):
        """Recover an orphaned pending recommendation job. Returns
        whether anything changed. Callers hold the lock."""
        job = checkpoint.get("recommendation_job")
        if not job or job["status"] != "pending":
            return False
        job["status"] = "unavailable"
        job["reason"] = "interrupted_by_restart"
        checkpoint["recommendation"] = {
            "status": "unavailable",
            "mode": "shadow_only",
            "reason": "interrupted_by_restart",
            "used_for_student_advice": False,
            "used_for_feedback": False}
        if (name == "end"
                and meta.get("feedback_bundle_version")
                == FEEDBACK_BUNDLE_VERSION
                and checkpoint.get("practice_context") is None):
            checkpoint["practice_context"] = empty_context(
                "unavailable")
        return True

    @staticmethod
    def _recover_diagnostics_alignment(name, checkpoint):
        """Refuse misaligned recorded diagnostics; never redrive them.
        Returns whether anything changed."""
        diag = checkpoint.get("diagnostics")
        if (not isinstance(diag, dict)
                or diag.get("status") != "ready"
                or (diag.get("checkpoint") == name
                    and diag.get("answer_count")
                    == CHECKPOINT_LENGTHS[name])):
            return False
        # Recorded against the wrong checkpoint; never redrive it,
        # just refuse to present it.
        checkpoint["diagnostics"] = {
            "status": "unavailable",
            "reason": "checkpoint_alignment_mismatch",
            "recorded_checkpoint": diag.get("checkpoint"),
            "recorded_answer_count": diag.get("answer_count")}
        checkpoint["diagnostics_job"] = {
            "status": "unavailable",
            "reason": "checkpoint_alignment_mismatch",
            "finished_at": utc_now()}
        return True

    def _recover_waiting_practice(self, meta):
        """Best-effort terminal practice for a single-mode end job
        that was waiting when the process died. Callers hold the
        lock."""
        try:
            evidence = meta["checkpoints"]["end"].get("evidence")
            if evidence is not None:
                self._terminal_practice(meta, evidence)
        except Exception:
            meta["checkpoints"]["end"]["practice_context"] = (
                empty_context("unavailable"))

    def _recover_provider_job(self, meta):
        """Recover the aggregate provider job. Returns whether the
        meta changed. Callers hold the lock."""
        job = meta.get("provider_job")
        if job and job["status"] == "pending":
            job["status"] = "fallback"
            job["interrupted"] = True
            end_cp = meta["checkpoints"].get("end", {})
            if (meta.get("feedback_bundle_version")
                    == FEEDBACK_BUNDLE_VERSION
                    and end_cp.get("practice_context") is None):
                end_cp["practice_context"] = empty_context(
                    "unavailable")
            self._publish_baselines(meta)
            return True
        if (job and job["status"] in (
                "waiting_for_practice", "waiting_for_diagnostics")
                and meta.get("feedback_bundle_version")
                == FEEDBACK_BUNDLE_VERSION):
            job["status"] = "fallback"
            job["interrupted"] = True
            if not self._is_multi(meta):
                self._recover_waiting_practice(meta)
            self._publish_baselines(meta)
            return True
        return False

    def close(self, wait=False):
        with self.lock:
            self._closing = True
        if self.cache is not None:
            self.cache.stop_retries()
        self.replays.close(wait=True)
        self.provider_pool.shutdown(wait=wait)
        self.diag_pool.shutdown(wait=wait)

    # ---------------- metadata persistence ----------------

    def _meta_path(self, session_id):
        return self.meta_dir / f"{session_id}.json"

    def _save_meta(self, meta):
        """Atomic metadata write with a unique temp file.

        Holds the service lock so concurrent readers cannot open the
        target mid-replace (Windows treats that as a sharing conflict)."""
        with self.lock:
            target = self._meta_path(meta["session_id"])
            tmp = target.with_name(
                f"{target.name}.{secrets.token_hex(6)}.tmp")
            with tmp.open("w", encoding="utf-8") as stream:
                json.dump(meta, stream, ensure_ascii=False, indent=2)
            tmp.replace(target)

    def _load_meta(self, session_id):
        # The lock pairs with _save_meta: readers never open the metadata
        # file while an atomic temp replace is in flight.
        with self.lock:
            if (not isinstance(session_id, str)
                    or not SESSION_ID_RE.fullmatch(session_id)):
                raise ValueError("invalid session id")
            path = self._meta_path(session_id)
            if not path.exists():
                raise FileNotFoundError("unknown demo session")
            meta = json.loads(path.read_text(encoding="utf-8"))
            context = self._context_for(meta)
            if (meta.get("session_id") != session_id
                    or meta.get("schema") != META_SCHEMA
                    or meta.get("synthetic") is not True
                    or meta.get("bank_sha256") != bank_fingerprint(context["bank"])
                    or meta.get("taxonomy_sha256")
                    != canonical_digest(context["taxonomy"])
                    or ("bank_provenance" in meta
                        and meta["bank_provenance"] != context["provenance"])
                    or (meta.get("bank_mode", "demo") != "demo"
                        and "bank_provenance" not in meta)
                    or meta.get("policy_version") != POLICY_VERSION):
                raise ValueError("metadata is incompatible")
            session = self.store.load(session_id)
            if (session["bank_sha256"] != meta["bank_sha256"]
                    or ("bank_mode" in session
                        and session["bank_mode"] != meta.get("bank_mode", "demo"))
                    or ("bank_provenance" in session
                        and session["bank_provenance"] != context["provenance"])
                    or ("taxonomy_sha256" in session
                        and session["taxonomy_sha256"] != meta["taxonomy_sha256"])):
                raise ValueError("session provenance is incompatible")
            return meta

    def _try_load_meta(self, session_id):
        """Load compatible metadata or None; never rewrites bad files."""
        try:
            return self._load_meta(session_id)
        except (OSError, ValueError, KeyError, TypeError,
                json.JSONDecodeError):
            return None

    def _authorize(self, session_id, token):
        meta = self._load_meta(session_id)
        if (not isinstance(token, str)
                or not hmac.compare_digest(
                    _hash_token(token), meta["token_sha256"])):
            raise PermissionError("invalid session credentials")
        return meta

    # ---------------- sessions ----------------

    def _new_meta(self, session_id, token, label=None, simulated=None,
                  bank_mode="demo", feedback_variant="question_first"):
        context = self._bank_context(bank_mode)
        meta = {
            "session_id": session_id,
            "schema": META_SCHEMA,
            "synthetic": True,
            "attestation": "synthetic_demo_not_real_learner_data",
            "label": label or f"{bank_mode}-{session_id[:8]}",
            "bank_mode": bank_mode, "bank_label": BANK_LABELS[bank_mode],
            "bank_provenance": copy.deepcopy(context["provenance"]),
            "simulated": simulated,
            "created_at": utc_now(),
            "bank_sha256": bank_fingerprint(context["bank"]),
            "taxonomy_sha256": canonical_digest(context["taxonomy"]),
            "policy_version": POLICY_VERSION,
            "token_sha256": _hash_token(token),
            "provider_job": {"status": "not_requested",
                             "provider_mode": self.provider_mode},
            "checkpoints": {
                "midpoint": {
                    "baseline_student": None, "student_message": None,
                    "student_message_version": None, "graph": None,
                    "diagnostics": None,
                    "diagnostics_job": {"status": "not_requested"}},
                "end": {
                    "evidence": None, "baseline_student": None,
                    "baseline_teacher": None, "student_review": None,
                    "teacher_review": None, "provider_job": None,
                    "graph": None, "diagnostics": None,
                    "diagnostics_job": {"status": "not_requested"},
                    "recommendation": None,
                    "recommendation_job": {"status": "not_requested"},
                    "practice_context": None}},
            "reviews": [],
            "feedback_release": None,
            "feedback_edit": None,
            "feedback_bundle_version": FEEDBACK_BUNDLE_VERSION,
            "feedback_variant": feedback_variant}
        if self.review_mode == "aligned_two_topic":
            meta["feedback_selection_version"] = (
                MULTI_REVIEW_POLICY_VERSION)
            meta["worked_review_version"] = (
                ASSESSMENT_WORKED_REVIEW_VERSION)
            meta["worked_example_selection_policy"] = (
                worked_example_selection.POLICY)
            meta["review_presentation_version"] = (
                WORKED_FIRST_VERSION
                if feedback_variant == "worked_first"
                else QUESTION_FIRST_VERSION)
            if feedback_variant == "worked_first":
                meta["feedback_context_version"] = (
                    WORKED_FIRST_CONTEXT_VERSION)
        return meta

    def create_session(self, label=None, simulated=None, replay=None, provider_mode=None,
                       bank_mode="demo", feedback_variant="question_first"):
        if (not isinstance(feedback_variant, str)
                or feedback_variant not in FEEDBACK_VARIANTS):
            raise ValueError("unknown feedback variant")
        if (feedback_variant == "worked_first"
                and self.review_mode != "aligned_two_topic"):
            raise ValueError(
                "worked-first feedback requires aligned_two_topic")
        if provider_mode not in (None, "rules", self.provider_mode):
            raise ValueError("provider mode is not configured")
        token = secrets.token_urlsafe(32)
        with self.lock:
            if replay and bank_mode != self.replay_bank_mode:
                raise ValueError("scenario replays require the configured replay bank")
            context = self._bank_context(bank_mode)
            started = context["mcqs"].start_session()
            session_id = started["session_id"]
            meta = self._new_meta(
                session_id, token, label=label, simulated=simulated,
                bank_mode=bank_mode, feedback_variant=feedback_variant)
            session = context["mcqs"].private_record(session_id)
            session["bank_mode"] = bank_mode
            session["bank_provenance"] = copy.deepcopy(context["provenance"])
            session["taxonomy_sha256"] = meta["taxonomy_sha256"]
            self.store.save(session)
            meta["replay"] = copy.deepcopy(replay)
            meta["provider_job"]["provider_mode"] = provider_mode or self.provider_mode
            self._save_meta(meta)
            snapshot = self._public_snapshot(session_id, meta)
        return {"session_id": session_id, "student_token": token,
                "snapshot": snapshot}

    def list_sessions(self):
        sessions = []
        with self.lock:
            for path in sorted(self.meta_dir.glob("*.json")):
                meta = self._try_load_meta(path.stem)
                if meta is None or meta.get("replay"):
                    continue
                try:
                    session = self._context_for(meta)["mcqs"].private_record(meta["session_id"])
                except Exception:
                    continue
                sessions.append({
                    "session_id": meta["session_id"],
                    "label": meta["label"],
                    "bank_mode": meta.get("bank_mode", "demo"),
                    "bank_label": BANK_LABELS[meta.get("bank_mode", "demo")],
                    "display_name": self._session_display(meta),
                    "created_at": meta["created_at"],
                    "status": session["status"],
                    "answered_count": len(session["responses"]),
                    "provider_job": dict(meta["provider_job"]),
                    "diagnostics": {
                        name: checkpoint["diagnostics_job"]["status"]
                        for name, checkpoint in meta["checkpoints"].items()},
                    "review_count": len(meta["reviews"])})
        sessions.sort(key=lambda row: row["created_at"], reverse=True)
        return sessions

    def _question_token(self, meta, position):
        return hmac.new(meta["token_sha256"].encode("ascii"),
                        f"{meta['session_id']}:{position}".encode("ascii"),
                        hashlib.sha256).hexdigest()

    def _current_question(self, session, meta):
        answered = len(session["responses"])
        if answered >= FULL_LENGTH or session["status"] == "complete":
            return None
        bank = self._context_for(meta)["bank"]
        question = bank["questions"][answered]
        return {key: copy.deepcopy(question[key]) for key in PUBLIC_QUESTION_KEYS} | {
            "question_token": self._question_token(meta, answered + 1),
            "skill_name": bank["skill_names"][question["skill_id"]],
            "position": answered + 1,
            "half": 1 if answered < HALF_LENGTH else 2}

    def _student_feedback(self, session_id, meta, checkpoint):
        """Return the student-visible draft message for a checkpoint."""
        block = meta["checkpoints"][checkpoint]
        job = block.get("phrasing_job") or {}
        if checkpoint == "midpoint" and job.get("status") == "pending":
            # The hosted call is queued, in flight, or between automatic
            # retries: publish a safe waiting notice instead of the
            # deterministic baseline so the student view matches the
            # teacher view. The truthy payload keeps the normal pause.
            return {"status": "pending", "sections": [],
                    "text": "Aitta is preparing your midpoint check-in. "
                            "You can continue with the assessment while "
                            "it finishes."}
        message = block.get("student_message")
        if checkpoint == "midpoint" and job.get("status") == "ready":
            draft = block.get("provider_draft")
            if draft is not None:
                message = draft
        if message is not None:
            result = {"sections": copy.deepcopy(message["sections"]),
                      "text": message["text"]}
            for key in ("total", "skills"):
                if key in message:
                    result[key] = copy.deepcopy(message[key])
            return result
        review = block["baseline_student"]
        if review is None:
            return None
        message = review["message"]
        return {"sections": copy.deepcopy(message["sections"]),
                "text": message["text"]}

    def _final_preview(self, meta):
        session = self._context_for(meta)["mcqs"].private_record(
            meta["session_id"])
        if session["status"] != "complete":
            return None
        end = meta["checkpoints"]["end"]
        integrated = (meta.get("feedback_bundle_version")
                      == FEEDBACK_BUNDLE_VERSION)
        multi = self._is_multi(meta)
        if not multi and integrated and end.get("practice_context") is None:
            return None
        edit = meta.get("feedback_edit")
        if edit is not None:
            return copy.deepcopy(edit["preview"])
        if multi:
            return self._multi_preview(meta, session)
        job = meta["provider_job"]
        # A stored student result is safe to show even when the
        # aggregate job fell back because the teacher leg failed.
        review = None
        if (job["status"] in ("ready", "fallback")
                and end.get("student_review") is not None):
            review = end["student_review"]
        if review is None:
            review = end.get("baseline_student")
        evidence = end.get("evidence")
        if review is None or evidence is None:
            return None
        sections = structured_sections(review)
        preview = {
            "template_version": (INTEGRATED_TEMPLATE_VERSION if integrated
                                 else TEMPLATE_VERSION),
            "sections": sections,
            "text": "\n\n".join(s["text"] for s in sections if s["text"]),
            "total": {"correct": evidence["total"]["correct"],
                      "out_of": evidence["total"]["out_of"]},
            "skills": [
                {"skill_id": row["skill_id"],
                 "skill_name": row["skill_name"],
                 "correct": row["correct"],
                 "out_of": row["out_of"]}
                for row in evidence["skills"]],
            "requires_educator_review": True}
        if integrated:
            selected = None
            if review is not None:
                selected = next(
                    (c for c in review.get("candidates") or []
                     if c.get("candidate_id")
                     == review.get("selected_candidate_id")), None)
            try:
                preview["practice"] = resolve_practice(
                    end["practice_context"], evidence,
                    candidate=selected)["card"]
            except Exception:
                preview["practice"] = {
                    "status": "unavailable",
                    "message": PRACTICE_STATUS_MESSAGES["unavailable"],
                    "notice": None, "question": None}
            if selected is not None:
                example = review_example(selected)
                if example is not None:
                    preview["review_example"] = example
        return preview

    def _multi_preview(self, meta, session):
        end = meta["checkpoints"]["end"]
        evidence = end.get("evidence")
        job = meta["provider_job"]
        review = None
        if (job["status"] in ("ready", "fallback")
                and end.get("student_review") is not None):
            review = end["student_review"]
        if review is None or evidence is None:
            return None
        sections = structured_sections(review)
        grounded = (meta.get("worked_review_version")
                    == ASSESSMENT_WORKED_REVIEW_VERSION)
        preview = {
            "template_version": (GROUNDED_TEMPLATE_VERSION if grounded
                                 else MULTI_INTEGRATED_TEMPLATE_VERSION),
            "sections": sections,
            "text": "\n\n".join(s["text"] for s in sections if s["text"]),
            "total": {"correct": evidence["total"]["correct"],
                      "out_of": evidence["total"]["out_of"]},
            "skills": [
                {"skill_id": row["skill_id"],
                 "skill_name": row["skill_name"],
                 "correct": row["correct"],
                 "out_of": row["out_of"]}
                for row in evidence["skills"]],
            "requires_educator_review": True}
        originals = review.get("selected_candidates") or []
        if grounded:
            preview["worked_review_version"] = review.get(
                "worked_review_version", ASSESSMENT_WORKED_REVIEW_VERSION)
            preview["review_examples"] = copy.deepcopy(
                review.get("review_examples") or [])
            # Private ranking data (probabilities, alternate
            # candidates) must never reach approved or student payloads.
            for example in preview["review_examples"]:
                example.pop("selection_provenance", None)
            if review.get("worked_review_status") is not None:
                preview["worked_review_status"] = review[
                    "worked_review_status"]
            if review.get("worked_review_notice") is not None:
                preview["worked_review_notice"] = review[
                    "worked_review_notice"]
        else:
            examples = []
            for candidate in originals:
                example = review_example(candidate)
                if example is not None:
                    example["for_subtopic_name"] = (
                        candidate["focus"] or {}).get("subtopic_name")
                    examples.append(example)
            if examples:
                preview["review_examples"] = examples
                preview["review_example"] = examples[0]
        by_id = {c["candidate_id"]: c for c in originals}
        activities = []
        for slot in end.get("practice_contexts") or []:
            candidate = by_id.get(slot.get("candidate_id"))
            try:
                card = resolve_practice(
                    slot["context"], evidence, candidate=candidate,
                    aligned=True)["card"]
            except Exception:
                card = {
                    "status": "unavailable",
                    "message": PRACTICE_STATUS_MESSAGES["unavailable"],
                    "notice": None, "question": None}
                focus = (candidate or {}).get("focus") or {}
                if focus.get("subtopic_name"):
                    card["topic_label"] = focus["subtopic_name"]
            activities.append(card)
        if activities:
            preview["practice_activities"] = activities
            preview["practice"] = activities[0]
        else:
            try:
                preview["practice"] = resolve_practice(
                    end.get("practice_context")
                    or empty_context("abstained"), evidence)["card"]
            except Exception:
                preview["practice"] = {
                    "status": "unavailable",
                    "message": PRACTICE_STATUS_MESSAGES["unavailable"],
                    "notice": None, "question": None}
        if grounded:
            if meta.get("feedback_variant") == "worked_first":
                preview["template_version"] = WORKED_FIRST_TEMPLATE_VERSION
                preview["review_presentation_version"] = (
                    WORKED_FIRST_VERSION)
                preview["feedback_variant"] = "worked_first"
                return preview
            if "feedback_variant" in meta:
                preview["feedback_variant"] = meta["feedback_variant"]
            preview = concise_student_preview(preview, originals)
            if meta.get("review_presentation_version") \
                    == QUESTION_FIRST_VERSION:
                return question_first_preview(preview)
            return preview
        return preview

    def _deterministic_preview(self, meta):
        """Teacher-only reference preview of the stored deterministic
        baseline, rendered through the same variant-aware layout as the
        main preview. The saved meta is never mutated and no provider
        call is made; teacher edits and provider wording stay confined
        to the main preview."""
        end = meta["checkpoints"]["end"]
        baseline = end.get("baseline_student")
        if baseline is None:
            return None
        copy_meta = copy.deepcopy(meta)
        copy_meta["feedback_edit"] = None
        copy_meta["checkpoints"]["end"]["student_review"] = (
            copy_meta["checkpoints"]["end"]["baseline_student"])
        copy_meta["provider_job"]["status"] = "fallback"
        return self._final_preview(copy_meta)

    @staticmethod
    def _worked_example_choices(end):
        """Teacher-only worked-example choices carrying full selection
        provenance; never part of the preview payload itself."""
        review = end.get("student_review") or {}
        choices = []
        for card in review.get("review_examples") or []:
            provenance = card.get("selection_provenance")
            if provenance is None:
                continue
            choices.append({
                "candidate_id": card.get("candidate_id"),
                "subtopic_name": card.get("for_subtopic_name"),
                **copy.deepcopy(provenance)})
        return choices

    @staticmethod
    def _delivery_status(session, meta):
        if session["status"] != "complete":
            return "not_ready"
        if meta.get("feedback_release"):
            return "released"
        return "awaiting_teacher_review"

    def _public_snapshot(self, session_id, meta=None):
        if meta is None:
            meta = self._load_meta(session_id)
        session = self._context_for(meta)["mcqs"].private_record(session_id)
        status = self._delivery_status(session, meta)
        delivery = {"status": status}
        released = None
        if status == "released":
            release = meta["feedback_release"]
            released = copy.deepcopy(release["released_feedback"])
            delivery["released_at"] = release["released_at"]
        return {
            "session_id": session_id,
            "synthetic": True,
            "bank_mode": meta.get("bank_mode", "demo"),
            "bank_label": BANK_LABELS[meta.get("bank_mode", "demo")],
            "status": session["status"],
            "answered_count": len(session["responses"]),
            "assessment_total": (
                {"correct": sum(row["correct"]
                                for row in session["responses"]),
                 "out_of": len(session["responses"])}
                if session["status"] == "complete" else None),
            "current_question": self._current_question(session, meta),
            "feedback": {
                "midpoint": self._student_feedback(
                    session_id, meta, "midpoint"),
                "end": released},
            "feedback_delivery": delivery,
            "provider_job": {
                "status": meta["provider_job"]["status"],
                "provider_mode": meta["provider_job"]["provider_mode"]}}

    def snapshot(self, session_id, token):
        with self.lock:
            meta = self._authorize(session_id, token)
            return self._public_snapshot(session_id, meta)

    # ---------------- submissions ----------------

    def submit_student_response(self, session_id, token, row):
        with self.lock:
            meta = self._authorize(session_id, token)
            if (not isinstance(row, dict)
                    or set(row) != {"question_token", "selected_index"}
                    or not isinstance(row["question_token"], str)
                    or not re.fullmatch(r"[a-f0-9]{64}", row["question_token"])):
                raise ValueError("response requires an opaque question token and selection")
            context = self._context_for(meta)
            session = context["mcqs"].private_record(session_id)
            answered = len(session["responses"])
            for position in (answered + 1, answered):
                if (1 <= position <= FULL_LENGTH
                        and hmac.compare_digest(row["question_token"],
                                                self._question_token(meta, position))):
                    return self.submit_response(session_id, token, {
                        "question_id": context["bank"]["questions"][position - 1]["question_id"],
                        "selected_index": row["selected_index"]})
            raise ValueError("response token does not match the current question")

    def submit_response(self, session_id, token, row):
        with self.lock:
            meta = self._authorize(session_id, token)
            mcqs = self._context_for(meta)["mcqs"]
            if (not isinstance(row, dict)
                    or set(row) != {"question_id", "selected_index"}
                    or not isinstance(row["question_id"], str)
                    or not isinstance(row["selected_index"], int)
                    or isinstance(row["selected_index"], bool)):
                raise ValueError(
                    "response must be {question_id, selected_index}")
            session = mcqs.private_record(session_id)
            responses = session["responses"]
            if (responses
                    and responses[-1]["question_id"] == row.get("question_id")):
                if responses[-1]["selected_index"] == row.get("selected_index"):
                    # Identical retry of the saved latest answer.
                    return self._public_snapshot(session_id, meta)
                raise ValueError("response does not match the required order")
            result = mcqs.submit_response(session_id, row)
            answered = result["position"]
            if answered == HALF_LENGTH:
                self._finish_midpoint(session_id, meta)
            elif answered == FULL_LENGTH:
                self._finish_end(session_id, meta)
            self._save_meta(meta)
            return self._public_snapshot(session_id, meta)

    def _rows(self, session_id):
        return self._context_for(self._load_meta(session_id))["mcqs"].private_response_rows(session_id)

    def _finish_midpoint(self, session_id, meta):
        # Rows are snapshotted at admission: the queued worker must never
        # observe a later session state.
        rows = copy.deepcopy(self._rows(session_id))
        context = self._context_for(meta)
        evidence = self._evidence(meta, rows)
        block = meta["checkpoints"]["midpoint"]
        block["baseline_student"] = run_feedback(
            evidence, "student", "midpoint")
        block["student_message"] = compose_midpoint_message(evidence)
        block["student_message_version"] = MIDPOINT_MESSAGE_VERSION
        block["graph"] = assessment_feedback_graph(
            context["bank"], context["taxonomy"], rows)
        self._admit_diagnostics(session_id, meta, "midpoint", rows)
        self._admit_midpoint_phrasing(
            session_id, meta, copy.deepcopy(evidence))

    def _finish_end(self, session_id, meta):
        rows = copy.deepcopy(self._rows(session_id))
        context = self._context_for(meta)
        evidence = self._evidence(meta, rows)
        end = meta["checkpoints"]["end"]
        end["evidence"] = evidence
        end["baseline_student"] = run_feedback(
            evidence, "student", "end")
        end["graph"] = assessment_feedback_graph(
            context["bank"], context["taxonomy"], rows)
        mode = meta["provider_job"]["provider_mode"]
        integrated = (meta.get("feedback_bundle_version")
                      == FEEDBACK_BUNDLE_VERSION)
        multi = self._is_multi(meta)
        self._admit_diagnostics(session_id, meta, "end", rows)
        if multi:
            try:
                end["baseline_student"] = run_multi_feedback(
                    evidence,
                    assessment_context=self._assessment_context(
                        meta, rows))
            except ValueError:
                review = run_multi_feedback(evidence)
                end["baseline_student"] = (
                    self._grounded_unavailable(review)
                    if self._is_grounded(meta) else review)
            self._admit_provider_job(session_id, meta, evidence)
            end["provider_job"] = copy.deepcopy(meta["provider_job"])
            return
        self._admit_recommendation(session_id, meta, rows)
        if integrated and end["recommendation_job"]["status"] == "pending":
            meta["provider_job"] = {
                "status": "waiting_for_practice", "provider_mode": mode,
                "queued_at": utc_now()}
        else:
            if integrated:
                self._terminal_practice(meta, evidence, rows)
            self._admit_provider_job(session_id, meta, evidence)
        end["provider_job"] = copy.deepcopy(meta["provider_job"])

    def _terminal_practice(self, meta, evidence, rows=None):
        end = meta["checkpoints"]["end"]
        if end.get("practice_context") is not None:
            return end["practice_context"]
        result = end.get("recommendation")
        if rows is None:
            rows = self._rows(meta["session_id"])
        context = self._context_for(meta)
        try:
            ctx, reason = project_practice_context(
                result, context["bank"], rows, evidence, self.recommender)
        except Exception:
            ctx = empty_context("unavailable")
            reason = "practice_context_validation_failed"
        end["practice_context"] = ctx
        if reason:
            end["practice_context_reason"] = reason
        for audience in ("student",):
            end[f"baseline_{audience}"] = run_feedback(
                evidence, audience, "end", practice_context=ctx)
        return ctx

    def _multi_fallback(self, meta, evidence, reason):
        end = meta["checkpoints"]["end"]
        contexts = self._multi_terminal(meta, evidence)
        end["student_review"] = self._multi_baseline_review(
            meta, evidence, reason,
            practice_contexts=[c["context"] for c in contexts])
        end["provider_job"] = copy.deepcopy(meta["provider_job"])

    def _provider_session_event(self, session_id, meta=None):
        """Persistent per-session cancel event; set once a replay case
        fails and re-armed on load via the persisted marker."""
        with self.lock:
            event = self._provider_session_stops.get(session_id)
            if event is None:
                event = threading.Event()
                self._provider_session_stops[session_id] = event
            if meta is None:
                try:
                    meta = self._load_meta(session_id)
                except FileNotFoundError:
                    meta = None
            if meta is not None and meta.get("background_work_cancelled"):
                event.set()
            return event

    @contextlib.contextmanager
    def _provider_session_scope(self, session_id):
        """Bind this thread's provider calls to the session's cancel
        event so a failed replay case stops future retries."""
        with self.lock:
            try:
                meta = self._load_meta(session_id)
            except FileNotFoundError:
                meta = None
            event = self._provider_session_event(session_id, meta)
            replay_id = ((meta or {}).get("replay") or {}).get("run_id")
        if self.cache is not None:
            with self.cache.session_scope(
                    session_id, replay_id=replay_id,
                    cancel_event=event):
                yield event
        else:
            yield event

    def cancel_replay_session(self, session_id,
                              reason="replay_case_failed"):
        """Mark a replay session's background work as cancelled: pending
        midpoint/end provider jobs stop and late results are discarded.
        The event is set before persistence; if saving fails the
        cancellation still stands and the error propagates."""
        if reason != "replay_case_failed":
            raise ValueError("unknown cancellation reason")
        with self.lock:
            event = self._provider_session_stops.get(session_id)
            if event is None:
                event = threading.Event()
                self._provider_session_stops[session_id] = event
            # Set before any metadata I/O: even if persistence fails
            # the session's provider work is cancelled.
            event.set()
            meta = self._load_meta(session_id)
            if not meta.get("background_work_cancelled"):
                meta["background_work_cancelled"] = {
                    "reason": reason, "cancelled_at": utc_now()}
            block = meta["checkpoints"]["midpoint"]
            phrasing = block.get("phrasing_job") or {}
            if phrasing.get("status") == "pending":
                phrasing.update({
                    "status": "cancelled",
                    "reason": "replay_case_failed",
                    "finished_at": utc_now()})
                block["phrasing_job"] = phrasing
                block["provider_draft"] = None
                block["provider_draft_version"] = None
            wjob = meta["checkpoints"]["end"].get("worked_example_job")
            if wjob and wjob.get("status") == "pending":
                wjob.update({"status": "cancelled",
                             "reason": "replay_case_failed",
                             "finished_at": utc_now()})
            job = meta["provider_job"]
            if job.get("status") in (
                    "pending", "waiting_for_practice",
                    "waiting_for_diagnostics"):
                job.update({"status": "fallback",
                            "reason": "replay_case_failed",
                            "finished_at": utc_now()})
                meta["checkpoints"]["end"]["provider_job"] = (
                    copy.deepcopy(job))
            self._save_meta(meta)

    def _admit_provider_job(self, session_id, meta, evidence):
        end = meta["checkpoints"]["end"]
        mode = meta["provider_job"]["provider_mode"]
        multi = self._is_multi(meta)
        ctx = (end.get("practice_context")
               if meta.get("feedback_bundle_version")
               == FEEDBACK_BUNDLE_VERSION else None)
        if self._provider_session_event(session_id, meta).is_set():
            meta["provider_job"] = {
                "status": "fallback",
                "provider_mode": mode,
                "queued_at": utc_now(), "finished_at": utc_now(),
                "reason": "replay_case_failed"}
            if multi:
                self._multi_fallback(meta, evidence,
                                     "replay_case_failed")
            else:
                for audience in ("student",):
                    end[f"{audience}_review"] = self._baseline_review(
                        evidence, audience, "replay_case_failed", ctx)
            end["provider_job"] = copy.deepcopy(meta["provider_job"])
            return
        if self._closing:
            meta["provider_job"] = {
                "status": "fallback",
                "provider_mode": mode,
                "queued_at": utc_now(), "finished_at": utc_now(),
                "reason": "service_closing"}
            if multi:
                self._multi_fallback(meta, evidence, "service_closing")
            else:
                for audience in ("student",):
                    end[f"{audience}_review"] = self._baseline_review(
                        evidence, audience, "service_closing", ctx)
            end["provider_job"] = copy.deepcopy(meta["provider_job"])
            return
        if self._pending_provider >= self.max_pending_jobs:
            meta["provider_job"] = {
                "status": "fallback",
                "provider_mode": mode,
                "queued_at": utc_now(), "finished_at": utc_now(),
                "reason": "demo_provider_queue_capacity"}
            if multi:
                self._multi_fallback(
                    meta, evidence, "demo_provider_queue_capacity")
            else:
                for audience in ("student",):
                    end[f"{audience}_review"] = self._baseline_review(
                        evidence, audience,
                        "demo_provider_queue_capacity", ctx)
        else:
            self._pending_provider += 1
            meta["provider_job"] = {
                "status": "pending", "provider_mode": mode,
                "queued_at": utc_now()}
            try:
                self.provider_pool.submit(
                    self._run_provider_job_for_session, session_id)
            except RuntimeError:
                self._pending_provider -= 1
                meta["provider_job"] = {
                    "status": "fallback",
                    "provider_mode": mode,
                    "queued_at": utc_now(), "finished_at": utc_now(),
                    "reason": "service_closing"}
                if multi:
                    self._multi_fallback(
                        meta, evidence, "service_closing")
                else:
                    for audience in ("student",):
                        end[f"{audience}_review"] = self._baseline_review(
                            evidence, audience, "service_closing", ctx)
        end["provider_job"] = copy.deepcopy(meta["provider_job"])

    def _admit_diagnostics(self, session_id, meta, checkpoint, rows):
        block = meta["checkpoints"][checkpoint]
        if self._pending_diag >= self.max_pending_jobs:
            block["diagnostics_job"] = {
                "status": "unavailable",
                "reason": "demo_diagnostics_queue_capacity",
                "finished_at": utc_now()}
            block["diagnostics"] = {
                "status": "unavailable",
                "reason": "demo_diagnostics_queue_capacity"}
            return
        self._pending_diag += 1
        block["diagnostics_job"] = {
            "status": "pending", "queued_at": utc_now()}
        self.diag_pool.submit(
            self._run_diagnostics, session_id, checkpoint, rows)

    def _admit_recommendation(self, session_id, meta, rows):
        """Opt-in private shadow practice pick after the 40th answer."""
        end = meta["checkpoints"]["end"]
        if self.recommender is None:
            end["recommendation_job"] = {
                "status": "disabled",
                "reason": "shadow_practice_not_configured",
                "finished_at": utc_now()}
            end["recommendation"] = {
                "status": "disabled", "mode": "shadow_only",
                "reason": "shadow_practice_not_configured",
                "used_for_student_advice": False,
                "used_for_feedback": False}
            return
        if self._pending_diag >= self.max_pending_jobs:
            end["recommendation_job"] = {
                "status": "unavailable",
                "reason": "demo_diagnostics_queue_capacity",
                "finished_at": utc_now()}
            end["recommendation"] = {
                "status": "unavailable", "mode": "shadow_only",
                "reason": "demo_diagnostics_queue_capacity",
                "used_for_student_advice": False,
                "used_for_feedback": False}
            return
        self._pending_diag += 1
        end["recommendation_job"] = {
            "status": "pending", "queued_at": utc_now()}
        self.diag_pool.submit(
            self._run_recommendation, session_id, copy.deepcopy(rows))

    def _admit_midpoint_phrasing(self, session_id, meta, evidence):
        """Queue the Aitta midpoint wording job.

        The deterministic check-in message is already saved by the
        caller and remains the immutable baseline/fallback; this job
        only fills the provider_draft record, which becomes the
        student-facing midpoint wording when the job succeeds. The
        evidence snapshot is frozen at the 20th answer by the caller.
        """
        block = meta["checkpoints"]["midpoint"]
        block["provider_draft"] = None
        block["provider_draft_version"] = None
        mode = meta["provider_job"]["provider_mode"]
        if mode != "hosted" or self.midpoint_generator is None:
            block["phrasing_job"] = {
                "status": "not_requested",
                "reason": ("rules_mode_no_providers" if mode != "hosted"
                           else "midpoint_provider_not_configured"),
                "finished_at": utc_now()}
            return
        if self._provider_session_event(session_id, meta).is_set():
            block["phrasing_job"] = {
                "status": "cancelled",
                "reason": "replay_case_failed",
                "queued_at": utc_now(), "finished_at": utc_now()}
            return
        if self._closing:
            block["phrasing_job"] = {
                "status": "fallback", "reason": "service_closing",
                "queued_at": utc_now(), "finished_at": utc_now()}
            return
        if self._pending_provider >= self.max_pending_jobs:
            block["phrasing_job"] = {
                "status": "unavailable",
                "reason": "demo_provider_queue_capacity",
                "finished_at": utc_now()}
            return
        self._pending_provider += 1
        block["phrasing_job"] = {"status": "pending",
                                 "queued_at": utc_now()}
        try:
            self.provider_pool.submit(
                self._run_midpoint_phrasing, session_id, evidence)
        except RuntimeError:
            self._pending_provider -= 1
            block["phrasing_job"] = {
                "status": "fallback", "reason": "service_closing",
                "queued_at": utc_now(), "finished_at": utc_now()}

    @staticmethod
    def _midpoint_exec_record(adapter):
        """Execution stand-in for midpoint adapters without a cache
        record; sanitized metadata only, never exception text."""
        record = {"status": "no_execution_record"}
        metadata = getattr(adapter, "last_metadata", None)
        if isinstance(metadata, dict):
            record["metadata"] = copy.deepcopy(metadata)
            if metadata.get("status") is not None:
                record["status"] = metadata["status"]
        draft = getattr(adapter, "last_rejected_draft", None)
        if draft is not None:
            record["rejected_draft"] = draft
        return record

    def _run_midpoint_phrasing(self, session_id, evidence):
        """Worker: one midpoint draft call; stores provider_draft.

        Scoped to the session's cancellation event: a replay case
        failure stops queued/scheduled calls before they run and
        suppresses any late publication."""
        adapter = self.midpoint_generator
        draft = None
        try:
            with self._provider_session_scope(session_id) as event:
                if event.is_set():
                    return
                with self._adapter_scope(adapter):
                    if event.is_set():
                        return
                    if hasattr(adapter, "execution"):
                        adapter.execution = None
                    self._reset_rejected_draft(adapter)
                    try:
                        reply = adapter.generate(
                            build_midpoint_input(evidence))
                        draft = compose_midpoint_draft(evidence, reply)
                        job = {"status": "ready",
                               "prompt_version": MIDPOINT_PROMPT_VERSION,
                               "message_version": MIDPOINT_DRAFT_VERSION,
                               "finished_at": utc_now()}
                    except SystemExit:
                        job = {"status": "fallback",
                               "reason": "demo_call_budget_exhausted",
                               "finished_at": utc_now()}
                    except Exception:
                        job = {"status": "fallback",
                               "reason": "provider_error",
                               "finished_at": utc_now()}
                    # Read per-thread adapter state while still inside
                    # the adapter scope so a later request cannot
                    # overwrite it.
                    execution = copy.deepcopy(
                        getattr(adapter, "execution", None))
                    if execution is None:
                        execution = self._midpoint_exec_record(adapter)
                if event.is_set():
                    return
                job["execution"] = execution
                with self.lock:
                    if event.is_set():
                        return
                    try:
                        meta = self._load_meta(session_id)
                    except FileNotFoundError:
                        return
                    block = meta["checkpoints"]["midpoint"]
                    if (block.get("phrasing_job") or {}).get(
                            "status") == "cancelled":
                        return
                    queued = (block.get("phrasing_job") or {}).get(
                        "queued_at")
                    if queued is not None:
                        job["queued_at"] = queued
                    block["provider_draft"] = draft
                    block["provider_draft_version"] = (
                        MIDPOINT_DRAFT_VERSION
                        if draft is not None else None)
                    block["phrasing_job"] = job
                    self._save_meta(meta)
        finally:
            with self.lock:
                self._pending_provider = max(
                    0, self._pending_provider - 1)

    # ---------------- background jobs ----------------

    def _run_diagnostics(self, session_id, checkpoint, rows):
        try:
            with self.lock:
                context = self._context_for(self._load_meta(session_id))
            result = self.diagnostics.evaluate(
                context["bank"], context["taxonomy"], rows)
            expected = CHECKPOINT_LENGTHS[checkpoint]
            if (result["status"] == "ready"
                    and (result.get("checkpoint") != checkpoint
                         or result.get("answer_count") != expected)):
                result = {
                    "status": "unavailable",
                    "reason": "checkpoint_alignment_mismatch",
                    "recorded_checkpoint": result.get("checkpoint"),
                    "recorded_answer_count":
                        result.get("answer_count")}
            status = result["status"]
            job = {"status": ("ready" if status == "ready"
                              else "unavailable"),
                   "finished_at": utc_now()}
            if status != "ready":
                job["reason"] = result.get("reason", "unavailable")
        except Exception:
            result = {"status": "unavailable",
                      "reason": "diagnostics_evaluation_failed"}
            job = {"status": "unavailable",
                   "reason": "diagnostics_evaluation_failed",
                   "finished_at": utc_now()}
        try:
            with self.lock:
                try:
                    meta = self._load_meta(session_id)
                except FileNotFoundError:
                    return
                meta["checkpoints"][checkpoint]["diagnostics"] = result
                meta["checkpoints"][checkpoint]["diagnostics_job"] = job
                self._save_meta(meta)
        finally:
            with self.lock:
                self._pending_diag = max(0, self._pending_diag - 1)

    def _run_recommendation(self, session_id, rows):
        try:
            with self.lock:
                meta = self._load_meta(session_id)
                context = self._context_for(meta)
            recommend = (self.recommender.recommend if meta.get("bank_mode", "demo") == "demo"
                         else self.recommender.recommend_research)
            result = recommend(
                copy.deepcopy(context["bank"]), copy.deepcopy(context["taxonomy"]), rows)
            if not isinstance(result, dict):
                raise ValueError("invalid recommendation result")
            result["used_for_student_advice"] = False
            result["used_for_feedback"] = False
            status = result.get("status")
            if status in ("selected", "abstained"):
                job = {"status": status, "finished_at": utc_now()}
            else:
                job = {"status": "unavailable",
                       "reason": result.get("reason", "unavailable"),
                       "finished_at": utc_now()}
                result = {"status": "unavailable", "mode": "shadow_only",
                          "reason": result.get("reason", "unavailable"),
                          "used_for_student_advice": False,
                          "used_for_feedback": False}
        except Exception:
            result = {"status": "unavailable", "mode": "shadow_only",
                      "reason": "future_recommendation_failed",
                      "used_for_student_advice": False,
                      "used_for_feedback": False}
            job = {"status": "unavailable",
                   "reason": "future_recommendation_failed",
                   "finished_at": utc_now()}
        try:
            with self.lock:
                try:
                    meta = self._load_meta(session_id)
                except FileNotFoundError:
                    return
                end = meta["checkpoints"]["end"]
                end["recommendation"] = result
                end["recommendation_job"] = job
                if (meta.get("feedback_bundle_version")
                        == FEEDBACK_BUNDLE_VERSION):
                    evidence = end.get("evidence") or self._evidence(
                        meta, rows)
                    self._terminal_practice(meta, evidence, rows)
                    if (meta["provider_job"]["status"]
                            == "waiting_for_practice"):
                        self._admit_provider_job(session_id, meta, evidence)
                self._save_meta(meta)
        finally:
            with self.lock:
                self._pending_diag = max(0, self._pending_diag - 1)

    @staticmethod
    def _is_multi(meta):
        return (meta.get("feedback_selection_version")
                == MULTI_REVIEW_POLICY_VERSION)

    def _multi_baseline_review(self, meta, evidence, reason,
                             practice_contexts=None, rows=None):
        end = meta["checkpoints"]["end"]
        recorded = end.get("multi_selection") or {}
        bundle = recorded.get("bundle")
        trace = recorded.get("trace") or {
            "selection_source": "rules", "selector_failed": False,
            "fallback_reason": None, "selection_matches_baseline": None}
        if bundle is not None:
            selection = {"bundle": bundle, "trace": trace}
        else:
            selection = None
            review = end.get("student_review") or {}
            candidate_id = review.get("selected_candidate_id")
            chosen = review.get("selected_candidates")
            if isinstance(candidate_id, str) and chosen:
                # A degraded record (selection metadata lost after a
                # queue/restart fallback) must not trigger a fresh
                # topic reselection: resolve the recorded candidate id
                # against the permitted bundles and keep it only when
                # the recorded candidates match exactly.
                try:
                    bundles = bundle_selection_payload(
                        evidence)["candidates"]
                except Exception:
                    bundles = []
                resolved = next(
                    (b for b in bundles
                     if b["candidate_id"] == candidate_id), None)
                if (resolved is not None
                        and resolved["review_candidates"] == chosen):
                    selection = {"bundle": resolved, "trace": trace}
        grounded = self._is_grounded(meta)
        if grounded:
            # Freeze the selection snapshot once so restart and queue
            # fallbacks stay reproducible instead of re-ranking later.
            self._freeze_worked_example_selection(meta, rows)
        try:
            assessment_ctx = self._assessment_context(meta, rows)
        except Exception:
            assessment_ctx = None
        try:
            review = run_multi_feedback(
                evidence, selection=selection,
                practice_contexts=practice_contexts,
                assessment_context=assessment_ctx)
        except ValueError:
            try:
                review = run_multi_feedback(
                    evidence, practice_contexts=practice_contexts,
                    assessment_context=assessment_ctx)
            except ValueError:
                review = run_multi_feedback(
                    evidence, practice_contexts=practice_contexts)
                assessment_ctx = None
        if grounded and (assessment_ctx is None
                         or "review_examples" not in review):
            review = self._grounded_unavailable(review)
        review["trace"]["fallback_reason"] = reason
        return review

    def _multi_contexts(self, context, rows, evidence,
                        candidates, result):
        contexts = []
        result = result or {}
        slots = {s["skill_id"]: s
                 for s in result.get("topic_selections") or []
                 if isinstance(s, dict)}
        for candidate in candidates:
            focus = candidate.get("focus")
            if focus is None:
                continue
            sid = focus["skill_id"]
            slot = slots.get(sid)
            if self.recommender is None:
                ctx = empty_context("disabled")
            elif slot is not None and slot.get("status") == "selected" \
                    and slot.get("selected_question_id"):
                view = dict(result)
                view["status"] = "selected"
                view["selected_question_id"] = slot[
                    "selected_question_id"]
                ctx, reason = project_practice_context(
                    view, context["bank"], rows, evidence, self.recommender)
                if ctx["status"] == "selected":
                    try:
                        import research_runtime
                        pool = research_runtime.load_current_practice_pool()
                        question = next(
                            q for q in pool["questions"]
                            if q["question_id"]
                            == ctx["selected_question_id"])
                        if question["skill_id"] != sid:
                            raise ValueError
                    except Exception:
                        ctx = empty_context("unavailable")
            elif slot is not None and slot.get("status") == "abstained":
                ctx = empty_context("abstained")
            elif (slot is not None
                  and slot.get("status") == "unavailable"):
                ctx = empty_context("unavailable")
            elif (not result
                  or result.get("status") in ("unavailable", "pending")):
                ctx = empty_context("unavailable")
            else:
                ctx = empty_context("abstained")
            contexts.append({"candidate_id": candidate["candidate_id"],
                             "skill_id": sid, "context": ctx})
        return contexts

    def _multi_terminal(self, meta, evidence, rows=None):
        end = meta["checkpoints"]["end"]
        if end.get("practice_contexts") is not None:
            return end["practice_contexts"]
        recorded = end.get("multi_selection") or {}
        if not recorded.get("bundle"):
            selection = select_review_bundle(evidence)
            end["multi_selection"] = {
                "bundle": copy.deepcopy(selection["bundle"]),
                "trace": copy.deepcopy(selection["trace"]),
                "selector_execution":
                    {"status": "rules_mode_no_providers"}}
            recorded = end["multi_selection"]
        if rows is None:
            rows = self._rows(meta["session_id"])
        context = self._context_for(meta)
        candidates = recorded["bundle"].get("review_candidates") or []
        result = end.get("recommendation")
        contexts = self._multi_contexts(
            context, rows, evidence, candidates, result)
        end["practice_contexts"] = contexts
        end["practice_context"] = (
            contexts[0]["context"] if contexts else empty_context(
                "unavailable" if self.recommender is not None
                else "disabled"))
        return contexts

    def _baseline_review(self, evidence, audience, reason,
                         practice_context=None):
        review = run_feedback(evidence, audience, "end",
                              practice_context=practice_context)
        review = copy.deepcopy(review)
        review["trace"]["fallback_reason"] = reason
        return review

    @contextlib.contextmanager
    def _adapter_scope(self, *adapters):
        if any(a is not None and not getattr(a, "thread_safe", False)
               for a in adapters):
            with self._provider_call_lock:
                yield
        else:
            yield

    def _jobs_complete(self, session_id):
        with self.lock:
            meta = self._load_meta(session_id)
            midpoint = meta["checkpoints"].get("midpoint", {})
            return (meta["provider_job"]["status"] in ("ready", "fallback")
                    and midpoint.get("phrasing_job", {}).get("status")
                    != "pending"
                    and (meta["checkpoints"]["end"]
                         .get("worked_example_job") or {})
                    .get("status") != "pending"
                    and all(c["diagnostics_job"]["status"] != "pending"
                            for c in meta["checkpoints"].values()))

    def _generator_exec_record(self, status):
        """Execution stand-in for adapters without an execution record."""
        record = {"status": status}
        # Per-thread slots only: the shared metadata/execution fallbacks
        # could attribute another thread's failure to this attempt.
        meta_local = getattr(self.generator, "_meta_local", None)
        code = ((meta_local.__dict__.get("value") or {})
                .get("rejection_code") if meta_local is not None else None)
        if code is not None:
            record["rejection_code"] = code
        draft = getattr(self.generator, "last_rejected_draft", None)
        if draft is not None:
            record["rejected_draft"] = draft
        return record

    @staticmethod
    def _reset_rejected_draft(adapter):
        clear = getattr(adapter, "_clear_rejected_draft", None)
        if clear is not None:
            clear()

    def _audience_result(self, evidence, audience, provider_mode=None,
                         practice_context=None, cancel_event=None):
        """One audience through providers; isolated failures per audience."""
        executions = {"selector": None, "generator": None}
        if (provider_mode or self.provider_mode) == "rules":
            review = self._baseline_review(
                evidence, audience, None, practice_context)
            review["trace"]["fallback_reason"] = None
            executions["selector"] = {"status": "rules_mode_no_providers"}
            executions["generator"] = {"status": "rules_mode_no_providers"}
            return review, executions
        with self._adapter_scope(self.selector, self.generator):
            if (cancel_event is not None
                    and cancel_event.is_set()):
                return None, {}
            try:
                if hasattr(self.selector, "execution"):
                    self.selector.execution = None
                if hasattr(self.generator, "execution"):
                    self.generator.execution = None
                self._reset_rejected_draft(self.generator)
                review = run_feedback(evidence, audience, "end",
                                      self.selector, self.generator,
                                      practice_context=practice_context)
                executions["selector"] = copy.deepcopy(
                    getattr(self.selector, "execution", None))
                executions["generator"] = copy.deepcopy(
                    getattr(self.generator, "execution", None))
                if executions["selector"] is None:
                    executions["selector"] = (
                        {"status": "local_single_candidate"}
                        if len(review["candidates"]) == 1
                        else {"status": "deterministic_rules"}
                        if self.selector is None
                        else {"status": "no_execution_record"})
                if executions["generator"] is None:
                    if (review["trace"]["fallback_reason"]
                            in SELECTOR_FAILURE_REASONS):
                        executions["generator"] = {
                            "status": "skipped_selector_failure"}
                    else:
                        executions["generator"] = (
                            self._generator_exec_record(
                                "no_execution_record"))
            except SystemExit:
                # Keep any completed stage record; only the stage that never
                # ran is marked budget_exhausted.
                review = self._baseline_review(
                    evidence, audience, "demo_call_budget_exhausted",
                    practice_context)
                executions = {
                    "selector": copy.deepcopy(
                        getattr(self.selector, "execution", None))
                    or {"status": "budget_exhausted"},
                    "generator": copy.deepcopy(
                        getattr(self.generator, "execution", None))
                    or {"status": "budget_exhausted"}}
            except Exception:
                review = self._baseline_review(
                    evidence, audience, "provider_error", practice_context)
                executions = {
                    "selector": copy.deepcopy(
                        getattr(self.selector, "execution", None))
                    or {"status": "worker_error"},
                    "generator": copy.deepcopy(
                        getattr(self.generator, "execution", None))
                    or self._generator_exec_record("worker_error")}
        return review, executions

    def _run_provider_job_for_session(self, session_id):
        with self._provider_session_scope(session_id):
            with self.lock:
                meta = self._load_meta(session_id)
                multi = self._is_multi(meta)
            (self._run_multi_provider_job if multi
             else self._run_provider_job)(session_id)

    def _run_provider_job(self, session_id):
        try:
            event = self._provider_session_event(session_id)
            if event.is_set():
                return
            try:
                with self.lock:
                    rows = self._rows(session_id)
                    meta = self._load_meta(session_id)
                    mode = meta["provider_job"]["provider_mode"]
                    practice_context = (meta["checkpoints"]["end"]
                                        .get("practice_context"))
                evidence = self._evidence(meta, rows)
                results = {}
                for audience in ("student",):
                    review, executions = self._audience_result(
                        evidence, audience, mode,
                        practice_context=practice_context,
                        cancel_event=event)
                    if event.is_set():
                        # Cancelled while the audience call was in
                        # flight; discard the late result entirely.
                        return
                    results[audience] = {"review": review,
                                         "executions": executions}
                any_fallback = any(
                    outcome["review"]["trace"]["fallback_reason"]
                    is not None for outcome in results.values())
                job_status = "fallback" if any_fallback else "ready"
                finished = utc_now()
            except Exception:
                results, job_status, finished = (
                    None, "fallback", utc_now())
            with self.lock:
                try:
                    meta = self._load_meta(session_id)
                except FileNotFoundError:
                    return
                if event.is_set():
                    return
                end = meta["checkpoints"]["end"]
                if results is None:
                    stored = end.get("evidence")
                    try:
                        evidence = (validate_evidence(stored) if stored
                                    else self._evidence(
                                        meta, self._rows(session_id)))
                    except Exception:
                        evidence = self._evidence(
                            meta, self._rows(session_id))
                    integrated = (meta.get("feedback_bundle_version")
                                  == FEEDBACK_BUNDLE_VERSION)
                    ctx = (end.get("practice_context")
                           if integrated else None)
                    try:
                        baselines = {
                            audience: self._baseline_review(
                                evidence, audience, "provider_error",
                                ctx)
                            for audience in ("student",)}
                    except Exception:
                        if integrated:
                            ctx = empty_context("unavailable")
                            end["practice_context"] = ctx
                            end["practice_context_reason"] = (
                                "provider_fallback_context_unavailable")
                        baselines = {}
                        for audience in ("student",):
                            review = run_feedback(
                                evidence, audience, "end",
                                practice_context=ctx)
                            review["trace"]["fallback_reason"] = (
                                "provider_error")
                            baselines[audience] = review
                        for audience in ("student",):
                            end[f"baseline_{audience}"] = copy.deepcopy(
                                baselines[audience])
                    for audience in ("student",):
                        end[f"{audience}_review"] = baselines[audience]
                    meta["provider_job"].update(
                        {"status": "fallback", "finished_at": finished,
                         "executions": {
                             a: {"status": "worker_error"}
                             for a in ("student",)}})
                else:
                    end["student_review"] = results["student"]["review"]
                    meta["provider_job"].update({
                        "status": job_status, "finished_at": finished,
                        "executions": {
                            audience: results[audience]["executions"]
                            for audience in results},
                        "prompt_versions": {
                            "selection": SELECTION_PROMPT_VERSION,
                            "generation": (
                                FULL_FEEDBACK_PROMPT_VERSION
                                if getattr(
                                    self.generator,
                                    "supports_full_feedback", False)
                                else PROMPT_VERSION)},
                        "policy_version": POLICY_VERSION})
                end["provider_job"] = copy.deepcopy(
                    meta["provider_job"])
                self._save_meta(meta)
        finally:
            with self.lock:
                self._pending_provider = max(
                    0, self._pending_provider - 1)

    def _aligned_recommendation(self, session_id, rows, skill_ids,
                                review_subtopic_ids=None):
        try:
            with self.lock:
                meta = self._load_meta(session_id)
                context = self._context_for(meta)
            result = self.recommender.recommend_for_reviews(
                copy.deepcopy(context["bank"]),
                copy.deepcopy(context["taxonomy"]), rows,
                list(skill_ids),
                research=meta.get("bank_mode", "demo") != "demo",
                review_subtopic_ids=copy.deepcopy(review_subtopic_ids))
            if not isinstance(result, dict):
                raise ValueError("invalid recommendation result")
            result["used_for_student_advice"] = False
            result["used_for_feedback"] = False
        except Exception:
            result = {"status": "unavailable", "mode": "shadow_only",
                      "reason": "future_recommendation_failed",
                      "used_for_student_advice": False,
                      "used_for_feedback": False,
                      "topic_selections": [
                          {"skill_id": sid, "status": "unavailable",
                           "reason": "future_recommendation_failed",
                           "selected_question_id": None,
                           "baseline_question_id": None}
                          for sid in skill_ids]}
        status = result.get("status")
        if status in ("selected", "abstained"):
            job = {"status": status, "finished_at": utc_now()}
        else:
            job = {"status": "unavailable",
                   "reason": result.get("reason", "unavailable"),
                   "finished_at": utc_now()}
        return result, job

    def _wait_for_worked_first_diagnostics(self, session_id):
        """Worked-first authoring waits for terminal model diagnostics so
        the provider context can carry their real (or explicitly
        unavailable) projections. Returns a checkpoint snapshot or None."""
        event = self._provider_session_event(session_id)
        while True:
            with self.lock:
                if self._closing or event.is_set():
                    return None
                try:
                    meta = self._load_meta(session_id)
                except FileNotFoundError:
                    return None
                checkpoints = meta["checkpoints"]
                if all(block["diagnostics_job"]["status"] != "pending"
                       for block in checkpoints.values()):
                    return copy.deepcopy(checkpoints)
                if (meta["provider_job"]["status"]
                        != "waiting_for_diagnostics"):
                    meta["provider_job"]["status"] = (
                        "waiting_for_diagnostics")
                    checkpoints["end"]["provider_job"] = copy.deepcopy(
                        meta["provider_job"])
                    self._save_meta(meta)
            time.sleep(0.05)

    def _run_multi_provider_job(self, session_id):
        selector_exec = generator_exec = None
        try:
            event = self._provider_session_event(session_id)
            if event.is_set():
                return
            try:
                with self.lock:
                    rows = self._rows(session_id)
                    meta = self._load_meta(session_id)
                    mode = meta["provider_job"]["provider_mode"]
                evidence = self._evidence(meta, rows)
                with self._adapter_scope(
                        self.selector if mode == "hosted" else None):
                    if event.is_set():
                        return
                    if (self.selector is not None
                            and hasattr(self.selector, "execution")):
                        self.selector.execution = None
                    selection = select_review_bundle(
                        evidence, self.selector
                        if mode == "hosted" else None)
                    if self.selector is not None:
                        selector_exec = copy.deepcopy(
                            getattr(self.selector, "execution", None))
                if selector_exec is None:
                    selector_exec = (
                        {"status": "local_single_candidate"}
                        if selection["trace"]["selection_source"]
                        == "rules_single_candidate"
                        else {"status": "rules_mode_no_providers"}
                        if mode == "rules"
                        else {"status": "deterministic_rules"}
                        if self.selector is None
                        else {"status": "no_execution_record"})
            except Exception:
                selection = selector_exec = None
            if selection is None:
                try:
                    with self.lock:
                        meta = self._load_meta(session_id)
                        evidence = (meta["checkpoints"]["end"].get("evidence")
                                    or self._evidence(meta, rows))
                    selection = select_review_bundle(evidence)
                    selection["trace"]["fallback_reason"] = (
                        selection["trace"]["fallback_reason"]
                        or "provider_error")
                except Exception:
                    selection = None
            with self.lock:
                try:
                    meta = self._load_meta(session_id)
                except FileNotFoundError:
                    return
                if event.is_set():
                    return
                end = meta["checkpoints"]["end"]
                if selection is not None:
                    end["multi_selection"] = {
                        "bundle": copy.deepcopy(selection["bundle"]),
                        "trace": copy.deepcopy(selection["trace"]),
                        "selector_execution": selector_exec}
                    meta["provider_job"]["status"] = (
                        "waiting_for_practice")
                    self._save_meta(meta)
            if selection is not None:
                originals = selection["bundle"]["review_candidates"]
                skill_ids = [c["focus"]["skill_id"] for c in originals
                             if c.get("focus")]
                review_subtopic_ids = {
                    c["focus"]["skill_id"]: c["focus"]["subtopic_id"]
                    for c in originals if c.get("focus")}
                if self.recommender is None:
                    rec_result = {
                        "status": "disabled", "mode": "shadow_only",
                        "reason": "shadow_practice_not_configured",
                        "used_for_student_advice": False,
                        "used_for_feedback": False}
                    rec_job = {"status": "disabled",
                               "reason": "shadow_practice_not_configured",
                               "finished_at": utc_now()}
                elif not skill_ids:
                    rec_result = {
                        "status": "abstained", "mode": "shadow_only",
                        "reason": "no_selected_review_skills",
                        "used_for_student_advice": False,
                        "used_for_feedback": False,
                        "topic_selections": []}
                    rec_job = {"status": "abstained",
                               "finished_at": utc_now()}
                else:
                    with self.lock:
                        try:
                            meta = self._load_meta(session_id)
                        except FileNotFoundError:
                            return
                        if event.is_set():
                            return
                        end = meta["checkpoints"]["end"]
                        if self._pending_diag >= self.max_pending_jobs:
                            rec_result = {
                                "status": "unavailable",
                                "mode": "shadow_only",
                                "reason": "demo_diagnostics_queue_capacity",
                                "used_for_student_advice": False,
                                "used_for_feedback": False}
                            rec_job = {
                                "status": "unavailable",
                                "reason": "demo_diagnostics_queue_capacity",
                                "finished_at": utc_now()}
                        else:
                            rec_result = rec_job = None
                            self._pending_diag += 1
                            end["recommendation_job"] = {
                                "status": "pending",
                                "queued_at": utc_now()}
                            self._save_meta(meta)
                    if rec_result is None:
                        try:
                            future = self.diag_pool.submit(
                                self._aligned_recommendation,
                                session_id, copy.deepcopy(rows),
                                list(skill_ids),
                                copy.deepcopy(review_subtopic_ids))
                            rec_result, rec_job = future.result()
                        finally:
                            with self.lock:
                                self._pending_diag = max(
                                    0, self._pending_diag - 1)
                with self.lock:
                    meta = self._load_meta(session_id)
                    if event.is_set():
                        return
                    end = meta["checkpoints"]["end"]
                    end["recommendation"] = rec_result
                    end["recommendation_job"] = rec_job
                    context = self._context_for(meta)
                    contexts = self._multi_contexts(
                        context, rows, evidence, originals, rec_result)
                    end["practice_contexts"] = contexts
                    end["practice_context"] = (
                        contexts[0]["context"] if contexts
                        else empty_context(
                            "abstained"
                            if self.recommender is not None
                            else "disabled"))
                    meta["provider_job"]["status"] = "pending"
                    self._save_meta(meta)
                diagnostic_blocks = None
                if (meta.get("worked_example_selection_policy")
                        == worked_example_selection.POLICY):
                    # Grounded worked-example selection needs terminal
                    # diagnostics regardless of variant or provider
                    # mode; the frozen snapshot is built afterwards.
                    diagnostic_blocks = (
                        self._wait_for_worked_first_diagnostics(
                            session_id))
                    if diagnostic_blocks is None:
                        # Closing or missing meta; restart recovery
                        # publishes the deterministic baseline.
                        return
                    with self.lock:
                        try:
                            meta = self._load_meta(session_id)
                        except FileNotFoundError:
                            return
                        if event.is_set():
                            return
                        self._freeze_worked_example_selection(meta, rows)
                        meta["provider_job"]["status"] = "pending"
                        meta["checkpoints"]["end"]["provider_job"] = (
                            copy.deepcopy(meta["provider_job"]))
                        self._save_meta(meta)
                try:
                    assessment_ctx = self._assessment_context(meta, rows)
                except Exception:
                    assessment_ctx = None
                worked_first_context = None
                if (meta.get("feedback_variant") == "worked_first"
                        and mode == "hosted"
                        and getattr(self.generator,
                                    "supports_full_feedback", False)
                        and assessment_ctx is not None):
                    if diagnostic_blocks is None:
                        diagnostic_blocks = (
                            self._wait_for_worked_first_diagnostics(
                                session_id))
                        if diagnostic_blocks is None:
                            # Closing or missing meta; restart recovery
                            # publishes the deterministic baseline.
                            return
                        with self.lock:
                            try:
                                meta = self._load_meta(session_id)
                            except FileNotFoundError:
                                return
                            if event.is_set():
                                return
                            meta["provider_job"]["status"] = "pending"
                            meta["checkpoints"]["end"]["provider_job"] = (
                                copy.deepcopy(meta["provider_job"]))
                            self._save_meta(meta)
                    worked_first_context = build_worked_first_context(
                        evidence, assessment_ctx, diagnostic_blocks)
                with self._adapter_scope(
                        self.generator if mode == "hosted" else None):
                    if event.is_set():
                        return
                    if (self.generator is not None
                            and hasattr(self.generator, "execution")):
                        self.generator.execution = None
                    self._reset_rejected_draft(self.generator)
                    full_narrative = (
                        meta.get("feedback_variant") == "worked_first")
                    try:
                        if self._is_grounded(meta) and assessment_ctx is None:
                            review = self._grounded_unavailable(
                                run_multi_feedback(
                                    evidence, selection=selection,
                                    practice_contexts=[
                                        c["context"] for c in contexts]))
                        else:
                            review = run_multi_feedback(
                                evidence, selection=selection,
                                generator=(
                                    self.generator
                                    if (mode == "hosted"
                                        and not selection["trace"]
                                        ["selector_failed"])
                                    else None),
                                practice_contexts=[
                                    c["context"] for c in contexts],
                                assessment_context=assessment_ctx,
                                provider_full_narrative=full_narrative,
                                worked_first_context=(
                                    worked_first_context))
                    except (Exception, SystemExit):
                        review = run_multi_feedback(
                            evidence, selection=selection,
                            practice_contexts=[
                                c["context"] for c in contexts],
                            assessment_context=assessment_ctx)
                        review["trace"]["fallback_reason"] = "provider_error"
                    generator_exec = (
                        copy.deepcopy(getattr(self.generator, "execution", None))
                        if self.generator is not None else None)
                if generator_exec is None:
                    generator_exec = (
                        {"status": "skipped_grounding_unavailable"}
                        if review.get("worked_review_status")
                        == "unavailable"
                        else {"status": "skipped_selector_failure"}
                        if selection["trace"]["selector_failed"]
                        else {"status": "rules_mode_no_providers"}
                        if mode == "rules"
                        else self._generator_exec_record(
                            "no_execution_record"))
                job_status = ("fallback"
                              if review["trace"]["fallback_reason"]
                              else "ready")
                baseline_review = None
                if event.is_set():
                    return
                if (meta.get("worked_example_selection_policy")
                        == worked_example_selection.POLICY):
                    # Refresh the deterministic baseline with the same
                    # frozen selection, selected bundle and practice
                    # contexts as the published draft; identical inputs
                    # give no reason to conceal a failure behind a
                    # stale earliest baseline. Runs outside the lock;
                    # genuine failure falls to _multi_worker_failure.
                    baseline_review = run_multi_feedback(
                        evidence, selection=selection,
                        practice_contexts=[
                            c["context"] for c in contexts],
                        assessment_context=assessment_ctx)
            with self.lock:
                try:
                    meta = self._load_meta(session_id)
                except FileNotFoundError:
                    return
                if event.is_set():
                    return
                end = meta["checkpoints"]["end"]
                if selection is None:
                    try:
                        self._multi_fallback(meta, end.get("evidence"),
                                             "provider_error")
                    except Exception:
                        pass
                    meta["provider_job"].update(
                        {"status": "fallback", "finished_at": utc_now(),
                         "executions": {"student": {
                             "selector": {"status": "worker_error"},
                             "generator": {"status": "worker_error"}}}})
                else:
                    end["student_review"] = review
                    if baseline_review is not None:
                        end["baseline_student"] = baseline_review
                    meta["provider_job"].update({
                        "status": job_status,
                        "finished_at": utc_now(),
                        "executions": {"student": {
                            "selector": selector_exec,
                            "generator": generator_exec}},
                        "prompt_versions": {
                            "selection": MULTI_SELECTION_PROMPT_VERSION,
                            "generation": (
                                review["trace"].get(
                                    "generation_prompt_version")
                                or (
                                    MULTI_FULL_FEEDBACK_PROMPT_VERSION
                                    if getattr(
                                        self.generator,
                                        "supports_full_feedback", False)
                                    else PROMPT_VERSION))}})
                end["provider_job"] = copy.deepcopy(meta["provider_job"])
                self._save_meta(meta)
        except Exception:
            self._multi_worker_failure(
                session_id, selector_exec, generator_exec)
        finally:
            with self.lock:
                self._pending_provider = max(
                    0, self._pending_provider - 1)

    def _multi_worker_failure(self, session_id, selector_exec=None,
                              generator_exec=None):
        with self.lock:
            try:
                meta = self._load_meta(session_id)
            except FileNotFoundError:
                return
            if self._provider_session_event(
                    session_id, meta).is_set():
                # A cancelled session keeps its cancellation record; a
                # racing worker exception must not replace it.
                return
            end = meta["checkpoints"]["end"]
            if meta["provider_job"].get("status") not in (
                    "pending", "waiting_for_practice",
                    "waiting_for_diagnostics"):
                return
            try:
                evidence = (end.get("evidence")
                            or self._evidence(
                                meta, self._rows(session_id)))
                rec_job = end.get("recommendation_job") or {}
                if (rec_job.get("status") in (None, "pending")
                        or not end.get("recommendation")):
                    if self.recommender is not None:
                        end["recommendation"] = {
                            "status": "unavailable",
                            "mode": "shadow_only",
                            "reason": "provider_error",
                            "used_for_student_advice": False,
                            "used_for_feedback": False}
                        end["recommendation_job"] = {
                            "status": "unavailable",
                            "reason": "provider_error",
                            "finished_at": utc_now()}
                self._multi_fallback(meta, evidence, "provider_error")
            except Exception:
                pass
            executions = ((meta["provider_job"].get("executions")
                           or {}).get("student") or {})
            recorded = (end.get("multi_selection") or {}).get(
                "selector_execution")
            meta["provider_job"].update({
                "status": "fallback", "finished_at": utc_now(),
                "executions": {"student": {
                    "selector": (executions.get("selector")
                                 or selector_exec or recorded
                                 or {"status": "worker_error"}),
                    "generator": (executions.get("generator")
                                  or generator_exec
                                  or {"status":
                                      "skipped_after_worker_error"})}}})
            end["provider_job"] = copy.deepcopy(meta["provider_job"])
            self._save_meta(meta)

    def _publish_baselines(self, meta):
        """Ensure restart-interrupted jobs keep usable baselines."""
        end = meta["checkpoints"]["end"]
        try:
            session = self._context_for(meta)["mcqs"].private_record(meta["session_id"])
            rows = [{"question_id": e["question_id"],
                     "selected_index": e["selected_index"]}
                    for e in session["responses"]]
            if len(rows) != FULL_LENGTH:
                return
            evidence = self._evidence(meta, rows)
            if end.get("evidence") is None:
                end["evidence"] = evidence
            if self._is_multi(meta):
                contexts = self._multi_terminal(meta, evidence, rows)
                if end.get("baseline_student") is None:
                    try:
                        end["baseline_student"] = run_multi_feedback(
                            evidence,
                            assessment_context=self._assessment_context(
                                meta, rows))
                    except ValueError:
                        review = run_multi_feedback(evidence)
                        end["baseline_student"] = (
                            self._grounded_unavailable(review)
                            if self._is_grounded(meta) else review)
                if end.get("student_review") is None:
                    end["student_review"] = self._multi_baseline_review(
                        meta, evidence, "interrupted_by_restart",
                        practice_contexts=[
                            c["context"] for c in contexts],
                        rows=rows)
                return
            ctx = (end.get("practice_context")
                   if meta.get("feedback_bundle_version")
                   == FEEDBACK_BUNDLE_VERSION else None)
            if ctx is not None:
                end["baseline_student"] = run_feedback(
                    evidence, "student", "end", practice_context=ctx)
            else:
                if end.get("baseline_student") is None:
                    end["baseline_student"] = run_feedback(
                        evidence, "student", "end")
            for audience in ("student",):
                if end.get(f"{audience}_review") is None:
                    end[f"{audience}_review"] = self._baseline_review(
                        evidence, audience, "interrupted_by_restart",
                        ctx)
        except Exception:
            return

    # ---------------- teacher views ----------------

    def _message_sha(self, review):
        return canonical_digest({
            "message": review["message"],
            "selected_candidate_id": review["selected_candidate_id"],
            "policy_version": review["trace"]["policy_version"],
            "bank_sha256": review["sanitized_evidence"]["bank_sha256"]})

    def teacher_config(self):
        multi = self.review_mode == "aligned_two_topic"
        return active_payload({
            "synthetic": True,
            "available_banks": self.available_banks(),
            "simulation_profiles": [{"id": name, **case_display(name)} for name in SIMULATION_PROFILES],
            "provider_mode": self.provider_mode,
            "review_mode": self.review_mode,
            "review_topic_limit": 2 if multi else 1,
            "practice_question_limit": 2 if multi else 1,
            "practice_alignment": (
                "selected_review_skills" if multi else None),
            "call_budget": {"limit": self.call_budget,
                            "used": self.cache.new_calls
                            if self.cache is not None else 0},
            "provider_timeout_seconds": self.provider_timeout,
            "diagnostics": self.diagnostics.state,
            "replay_bank_mode": self.replay_bank_mode,
            "replay_bank_label": BANK_LABELS[self.replay_bank_mode],
            "shadow_practice": self._shadow_config(),
            "bank_sha256": bank_fingerprint(self.bank),
            "taxonomy_sha256": canonical_digest(self.taxonomy),
            "policy_version": POLICY_VERSION,
            "review_selection_policy_version": (
                MULTI_REVIEW_POLICY_VERSION if multi else POLICY_VERSION),
            "review_selection_execution": (
                "deterministic_rules" if self.selector is None
                else "injected_selector_test_seam"),
            "selection_instructions_status": (
                "archived_reference_not_executed"),
            "selection_instructions": (
                MULTI_SELECTION_INSTRUCTIONS if multi
                else SELECTION_INSTRUCTIONS),
            "generation_instructions": (
                MULTI_FULL_FEEDBACK_INSTRUCTIONS if multi
                else FULL_FEEDBACK_INSTRUCTIONS),
            "generation_prompt_version": (
                MULTI_FULL_FEEDBACK_PROMPT_VERSION if multi
                else FULL_FEEDBACK_PROMPT_VERSION),
            "worked_first_generation_prompt_version": (
                WORKED_FIRST_GENERATION_PROMPT_VERSION
                if multi else None),
            "worked_first_generation_context_version": (
                WORKED_FIRST_CONTEXT_VERSION if multi else None),
            "worked_review_version": (
                ASSESSMENT_WORKED_REVIEW_VERSION if multi else None),
            "worked_solution_version": (
                worked_review_math.SOLUTION_VERSION if multi else None),
            "questions": copy.deepcopy(self.bank["questions"]),
            "skill_names": copy.deepcopy(self.bank["skill_names"]),
            "taxonomy": copy.deepcopy(self.taxonomy),
            "review_contract": {
                "version":
                    educator_review_contract.REVIEW_CONTRACT_VERSION,
                "fields": educator_review_contract.REVIEW_FIELDS,
                "notice": educator_review_contract.REVIEW_NOTICE},
            "disclaimer": "local synthetic demo; not production auth"})

    def _session_display(self, meta):
        if meta.get("replay"):
            return meta["label"]
        simulated = meta.get("simulated")
        name = case_display(simulated["profile"])["display_name"] if simulated else "Manual synthetic assessment"
        return f"{BANK_LABELS[meta.get('bank_mode', 'demo')]} · {name}"

    def _question_records(self, responses, include_subtopics=True, context=None):
        context = context or self._bank_context()
        questions = {q["question_id"]: q for q in context["bank"]["questions"]}
        subtopics = {qid: (sub["id"], sub["name"]) for topic in context["taxonomy"]["topics"]
                     for sub in topic["subtopics"] for qid in sub["question_ids"]}
        rows = []
        for position, event in enumerate(responses, 1):
            question = questions[event["question_id"]]
            sub_id, sub_name = subtopics[event["question_id"]] if include_subtopics else (None, "Historical label unavailable")
            rows.append({"position": position, "question_id": event["question_id"],
                         "skill_id": question["skill_id"], "text": question["text"],
                         "options": question["options"], "selected_index": event["selected_index"],
                         "answer_index": question["answer_index"],
                         "correct": event["selected_index"] == question["answer_index"],
                         "answered_at": event.get("answered_at"),
                         "subtopic_id": sub_id, "subtopic_name": sub_name})
        return rows

    def scenario_detail(self, case_id):
        case = self.research.scenario(case_id)
        library = self.research.library()
        context = self._bank_context(self.replay_bank_mode)
        end_packages = {p["audience"]: p for p in case["packages"] if p["checkpoint"] == "end"}
        evidence = end_packages["student"]["review"]["sanitized_evidence"]
        identity = case_display(case["name"], evidence)
        try:
            responses = self.replays.source_cases()[case_id]
            rows = self._question_records(
                responses,
                library["source"].get("taxonomy_sha256") == canonical_digest(context["taxonomy"]),
                context)
            response_notice = "Original saved answer sequence joined to the matching private bank."
        except (OSError, ValueError, KeyError, TypeError):
            rows = []
            response_notice = "Original response source is unavailable or does not match the current bank; individual answers cannot be shown."
        stored_diag = case.get("research_diagnostics", {})
        checkpoints = {}
        for checkpoint, count in (("midpoint", 20), ("end", 40)):
            graph = case.get("assessment_graph", {}).get("checkpoints", {}).get(checkpoint)
            kt = copy.deepcopy(stored_diag.get("kt", {}))
            probs = kt.get("p_correct_before_each_answer", [])
            available = len(probs) == 40
            kt["p_correct_before_each_answer"] = probs[:count]
            kt["coverage_scope"] = "original_full_40_question_trace"
            kt["provenance"] = copy.deepcopy(library.get("diagnostic_provenance", {}))
            diag = {"status": "ready" if available else "unavailable",
                    "mode": "frozen_replay_no_model_rerun",
                    "checkpoint": checkpoint, "answer_count": count,
                    "used_for_student_advice": False, "kt": kt,
                    "scope_warning": "Historical saved model outputs; not recomputed and not validated mastery evidence."}
            checkpoints[checkpoint] = {"graph": graph, "diagnostics": diag,
                                       "diagnostics_job": {"status": diag["status"]}}
        midpoint = next((p["review"] for p in case["packages"] if p["checkpoint"] == "midpoint"), None)
        checkpoints["midpoint"]["baseline_student"] = copy.deepcopy(midpoint)
        end = checkpoints["end"]
        end["evidence"] = copy.deepcopy(evidence)
        executions = {}
        for audience, package in end_packages.items():
            review = package["review"]
            baseline = copy.deepcopy(review)
            baseline["message"] = copy.deepcopy(review["template_baseline"])
            baseline["selected_candidate_id"] = review["baseline_candidate_id"]
            baseline["trace"].update({"selection_source": "rules_baseline", "fallback_reason": None})
            end[f"baseline_{audience}"] = baseline
            end[f"{audience}_review"] = copy.deepcopy(review)
            executions[audience] = {"selector": package.get("selector_execution"),
                                   "generator": package.get("generator_execution")}
        case.update(identity)
        case["detail"] = {"session_id": case_id, "label": case["name"], **identity,
                          "bank_mode": self.replay_bank_mode,
                          "bank_label": BANK_LABELS[self.replay_bank_mode],
                          "content_context": self._teacher_context(self.replay_bank_mode),
                          "status": "complete", "answered_count": 40,
                          "read_only": True, "source_mode": "retained_original",
                          "observed_total": copy.deepcopy(evidence["total"]),
                          "question_responses": rows, "response_notice": response_notice,
                          "checkpoints": checkpoints, "review_hashes": {}, "reviews": [],
                          "review_contract": self.teacher_config()["review_contract"],
                          "provider_job": {"status": "ready", "provider_mode": library["policy"].get("provider_mode", "recorded"),
                                           "executions": executions},
                          "provenance": {"report_sha256": library["report_sha256"], "source": library["source"],
                                         "policy": library["policy"], "mode": "retained_original_not_recomputed",
                                         "bank": copy.deepcopy(context["provenance"])}}
        return active_payload(case)

    def teacher_session(self, session_id):
        with self.lock:
            meta = self._load_meta(session_id)
            context = self._context_for(meta)
            session = context["mcqs"].private_record(session_id)
            rows = self._question_records(session["responses"], context=context)
            checkpoints = copy.deepcopy(meta["checkpoints"])
            end = checkpoints.get("end", {})
            review_hashes = {}
            if end.get("student_review") is not None:
                review_hashes["student"] = self._message_sha(
                    end["student_review"])
            if end.get("teacher_review") is not None:
                review_hashes["teacher"] = self._message_sha(
                    end["teacher_review"])
            review_trace = ((end.get("student_review") or {}).get("trace")
                            or {})
            provenance = {
                **copy.deepcopy(context["provenance"]),
                "bank_sha256": meta["bank_sha256"],
                "taxonomy_sha256": meta["taxonomy_sha256"],
                "policy_version": meta["policy_version"],
                "review_selection_policy_version": (
                    MULTI_REVIEW_POLICY_VERSION
                    if self._is_multi(meta) else POLICY_VERSION),
                "review_selection_execution": (
                    review_trace.get("selection_source")
                    or "not_recorded"),
                "feedback_variant": meta.get("feedback_variant"),
                "selection_prompt_version": (
                    MULTI_SELECTION_PROMPT_VERSION
                    if self._is_multi(meta) else SELECTION_PROMPT_VERSION),
                "generation_prompt_version": PROMPT_VERSION}
            if review_trace.get("generation_prompt_version"):
                provenance["generation_prompt_version"] = review_trace[
                    "generation_prompt_version"]
            diagnostics = end.get("diagnostics") or {}
            if (isinstance(diagnostics, dict)
                    and isinstance(diagnostics.get("kt"), dict)):
                provenance["diagnostics"] = copy.deepcopy(
                    diagnostics["kt"].get("provenance", {}))
            preview = self._final_preview(meta)
            delivery = {
                "status": self._delivery_status(session, meta),
                "integrated": (meta.get("feedback_bundle_version")
                               == FEEDBACK_BUNDLE_VERSION),
                "feedback_variant": meta.get("feedback_variant"),
                "review_mode": ("aligned_two_topic" if self._is_multi(meta)
                                else "legacy_single"),
                "practice_status": None,
                "practice_status_message": None,
                "worked_example_job": copy.deepcopy(
                    end.get("worked_example_job")),
                "preview": preview,
                "preview_sha256": (canonical_digest(preview)
                                   if preview is not None else None),
                "deterministic_preview":
                    self._deterministic_preview(meta),
                "worked_example_choices": (
                    self._worked_example_choices(end)),
                "released_feedback": None,
                "released_at": None,
                "reviewer_label": None}
            edit = meta.get("feedback_edit")
            if edit is not None:
                delivery["preview_source"] = "teacher_edited"
                delivery["edited_at"] = edit["saved_at"]
                delivery["edit_reviewer_label"] = edit["reviewer_label"]
            else:
                preview_source = "deterministic"
                if (meta["provider_job"]["status"] in ("ready", "fallback")
                        and end.get("student_review") is not None):
                    preview_source = review_trace.get(
                        "phrasing_source") or "deterministic"
                delivery["preview_source"] = preview_source
            if delivery["integrated"]:
                ctx = end.get("practice_context")
                if ctx is None:
                    delivery["practice_status"] = "pending"
                    delivery["practice_status_message"] = (
                        PRACTICE_STATUS_MESSAGES["pending"])
                else:
                    delivery["practice_status"] = ctx["status"]
                    delivery["practice_status_message"] = (
                        PRACTICE_STATUS_MESSAGES[ctx["status"]])
                    if ctx["status"] == "disabled":
                        delivery["practice_hint"] = (
                            self._shadow_config()["disabled_hint"])
            release = meta.get("feedback_release")
            if release:
                delivery.update({
                    "released_feedback": copy.deepcopy(
                        release["released_feedback"]),
                    "released_at": release["released_at"],
                    "reviewer_label": release["reviewer_label"],
                    "released_preview_sha256":
                        release["preview_sha256"]})
            return active_payload({
                "session_id": session_id,
                "bank_mode": meta.get("bank_mode", "demo"),
                "bank_label": BANK_LABELS[meta.get("bank_mode", "demo")],
                "content_context": self._teacher_context(meta.get("bank_mode", "demo")),
                "label": meta["label"],
                "display_name": self._session_display(meta),
                "created_at": meta["created_at"],
                "replay": copy.deepcopy(meta.get("replay")),
                "simulated": meta["simulated"],
                "feedback_variant": meta.get("feedback_variant"),
                "status": session["status"],
                "answered_count": len(session["responses"]),
                "observed_total": {
                    "correct": sum(e["correct"] for e in
                                   session["responses"]),
                    "out_of": len(session["responses"])},
                "question_responses": rows,
                "checkpoints": checkpoints,
                "review_hashes": review_hashes,
                "review_contract": self.teacher_config()["review_contract"],
                "reviews": copy.deepcopy(meta["reviews"]),
                "provider_job": copy.deepcopy(meta["provider_job"]),
                "worked_example_selection_history": copy.deepcopy(
                    meta.get("worked_example_selection_history", [])),
                "feedback_delivery": delivery,
                "provenance": provenance})

    def _shadow_config(self):
        config = {"enabled": self.recommender is not None,
                  "mode": "shadow_only",
                  "target_band": None,
                  "pool_question_count": None,
                  "disabled_hint": None}
        if self.recommender is not None:
            band = getattr(self.recommender, "band", None)
            if band is not None:
                config["target_band"] = list(band)
            pool = getattr(self.recommender, "pool", None)
            if isinstance(pool, dict) and isinstance(
                    pool.get("questions"), list):
                config["pool_question_count"] = len(pool["questions"])
        else:
            config["disabled_hint"] = (
                "Start the demo with the current practice pool and an "
                "explicit target band to enable KT practice selection.")
        return config

    @staticmethod
    def _teacher_context(bank_mode):
        try:
            return teacher_content_context(bank_mode)
        except (OSError, ValueError, KeyError):
            return {"status": "unavailable", "reason": "current_content_binding_unavailable"}

    # ---------------- educator reviews ----------------

    def add_review(self, session_id, body):
        if not isinstance(body, dict) or not set(body) <= {
                "audience", "message_sha256", "judgments",
                "note", "reviewer_label"}:
            raise ValueError("review body has unexpected fields")
        audience = body.get("audience")
        if audience not in ("student", "teacher"):
            raise ValueError("audience must be student or teacher")
        digest = body.get("message_sha256")
        if (not isinstance(digest, str)
                or len(digest) != 64 or any(
                    ch not in "0123456789abcdef" for ch in digest)):
            raise ValueError("message_sha256 must be a sha256 hex digest")
        judgments = body.get("judgments")
        allowed = {field["id"]: set(field["choices"])
                   for field in educator_review_contract.REVIEW_FIELDS}
        if (not isinstance(judgments, dict)
                or set(judgments) != set(allowed)):
            raise ValueError("judgments must answer every review field")
        for field_id, choice in judgments.items():
            if (not isinstance(choice, str)
                    or choice not in allowed[field_id]):
                raise ValueError(f"judgment {field_id} is not permitted")
        note = _strict_text(body.get("note", ""), "note", 3000)
        reviewer = _strict_text(
            body.get("reviewer_label", "anonymous"), "reviewer_label", 80)
        with self.lock:
            meta = self._load_meta(session_id)
            session = self._context_for(meta)["mcqs"].private_record(session_id)
            if session["status"] != "complete":
                raise ValueError(
                    "reviews require a completed end checkpoint")
            review = meta["checkpoints"]["end"].get(
                f"{audience}_review")
            if review is None:
                raise ConflictError("feedback is not ready for review")
            current = self._message_sha(review)
            if current != digest:
                raise ConflictError(
                    "stale feedback revision", current=current)
            entry = {
                "recorded_at": utc_now(), "audience": audience,
                "message_sha256": current,
                "selected_candidate_id":
                    review["selected_candidate_id"],
                "policy_version": review["trace"]["policy_version"],
                "review_contract_version":
                    educator_review_contract.REVIEW_CONTRACT_VERSION,
                "judgments": dict(judgments), "note": note,
                "reviewer_label": reviewer,
                "scored": False,
                "approves_learner_delivery": False}
            meta["reviews"].append(entry)
            self._save_meta(meta)
            if meta.get("replay"):
                self.replays.sync_reviews(session_id, meta["reviews"])
            return {"recorded": True, "review": copy.deepcopy(entry)}

    @staticmethod
    def _worked_example_job_pending(meta):
        return ((meta["checkpoints"]["end"]
                 .get("worked_example_job") or {})
                .get("status") == "pending")

    def release_feedback(self, session_id, body):
        if (not isinstance(body, dict)
                or not set(body) <= {"message_sha256", "reviewer_label"}
                or "message_sha256" not in body):
            raise ValueError("release body has unexpected fields")
        digest = body["message_sha256"]
        if (not isinstance(digest, str)
                or len(digest) != 64 or any(
                    ch not in "0123456789abcdef" for ch in digest)):
            raise ValueError("message_sha256 must be a sha256 hex digest")
        reviewer = _strict_text(
            body.get("reviewer_label", "anonymous"), "reviewer_label", 80)
        with self.lock:
            meta = self._load_meta(session_id)
            session = self._context_for(meta)["mcqs"].private_record(
                session_id)
            if session["status"] != "complete":
                raise ValueError(
                    "release requires a completed session")
            if self._worked_example_job_pending(meta):
                raise ConflictError(
                    "a worked example update is still in progress")
            preview = self._final_preview(meta)
            if preview is None:
                raise ConflictError("feedback is not ready for release")
            current = canonical_digest(preview)
            if current != digest:
                raise ConflictError(
                    "stale feedback revision", current=current)
            existing = meta.get("feedback_release")
            if existing and existing["preview_sha256"] == current:
                return {"released": True,
                        "released_at": existing["released_at"],
                        "preview_sha256": current}
            payload = copy.deepcopy(preview)
            payload["requires_educator_review"] = False
            meta["feedback_release"] = {
                "released_feedback": payload,
                "released_at": utc_now(),
                "reviewer_label": reviewer,
                "preview_sha256": current}
            self._save_meta(meta)
            return {"released": True,
                    "released_at":
                        meta["feedback_release"]["released_at"],
                    "preview_sha256": current}

    def select_worked_example(self, session_id, body):
        """Replace one worked-example card before approval, preserving
        the feedback narrative and its generation provenance."""
        if (not isinstance(body, dict)
                or not {"message_sha256", "candidate_id",
                        "question_id"} <= set(body)
                or not set(body) <= {
                    "message_sha256", "candidate_id", "question_id",
                    "reviewer_label"}):
            raise ValueError(
                "worked-example selection body has unexpected fields")
        digest = body["message_sha256"]
        if (not isinstance(digest, str)
                or len(digest) != 64 or any(
                    ch not in "0123456789abcdef" for ch in digest)):
            raise ValueError(
                "message_sha256 must be a sha256 hex digest")
        candidate_id = body["candidate_id"]
        question_id = body["question_id"]
        if not isinstance(candidate_id, str) or not candidate_id:
            raise ValueError("candidate_id must be a nonempty string")
        if not isinstance(question_id, str) or not question_id:
            raise ValueError("question_id must be a nonempty string")
        reviewer = _strict_text(
            body.get("reviewer_label", "anonymous"), "reviewer_label", 80)
        with self.lock:
            meta = self._load_meta(session_id)
            session = self._context_for(meta)["mcqs"].private_record(
                session_id)
            if session["status"] != "complete":
                raise ValueError(
                    "worked-example selection requires a completed "
                    "session")
            if (meta.get("worked_example_selection_policy")
                    != worked_example_selection.POLICY):
                raise ValueError(
                    "worked-example selection requires the grounded "
                    "selection policy")
            if meta["provider_job"].get("status") not in (
                    "ready", "fallback"):
                raise ConflictError(
                    "feedback is not ready for worked-example "
                    "selection")
            if meta.get("feedback_release"):
                raise ConflictError("feedback is already released")
            preview = self._final_preview(meta)
            if preview is None:
                raise ConflictError(
                    "feedback is not ready for worked-example "
                    "selection")
            current = canonical_digest(preview)
            if current != digest:
                raise ConflictError(
                    "stale feedback revision", current=current)
            end = meta["checkpoints"]["end"]
            review = end.get("student_review")
            if review is None:
                raise ConflictError(
                    "feedback is not ready for worked-example "
                    "selection")
            matched = next(
                (c for c in review.get("selected_candidates") or []
                 if c.get("candidate_id") == candidate_id
                 and c.get("focus")), None)
            if matched is None:
                raise ValueError(
                    "candidate_id must be a selected review candidate "
                    "with a focus")
            snapshot = end.get(worked_example_selection.CONTEXT_KEY)
            if not isinstance(snapshot, dict):
                raise ConflictError(
                    "worked-example selection snapshot unavailable")
            card = next(
                (c for c in review.get("review_examples") or []
                 if c.get("candidate_id") == candidate_id), None)
            if (card or {}).get("question_id") == question_id:
                # Already selected: idempotent, no draft reset.
                return {"saved": True, "preview_sha256": current}
            if self._worked_example_job_pending(meta):
                raise ConflictError(
                    "a worked example update is still in progress")
            edit = meta.get("feedback_edit")
            if (isinstance(edit, dict)
                    and isinstance(
                        (edit.get("preview") or {})
                        .get("document_text"), str)):
                # A saved free-form document cannot be safely patched
                # per example; the teacher must remove it or switch to
                # structured fields first.
                raise ConflictError(
                    "a saved teacher-edited document cannot be updated "
                    "per worked example; save wording as structured "
                    "fields or discard the document first")
            rows = self._rows(session_id)
            context = self._context_for(meta)
            cloned = copy.deepcopy(snapshot)
            cloned["overrides"] = dict(cloned.get("overrides") or {})
            cloned["overrides"][candidate_id] = question_id
            # Reject the override against the frozen assessment before
            # any metadata changes.
            worked_example_selection.validate_selection(
                cloned, context["bank"], rows)
            recorded = end.get("multi_selection") or {}
            if not recorded.get("bundle"):
                raise ConflictError(
                    "selected review bundle is unavailable")
            selection = {
                "bundle": copy.deepcopy(recorded["bundle"]),
                "trace": copy.deepcopy(recorded.get("trace") or {})}
            evidence = (end.get("evidence")
                        or self._evidence(meta, rows))
            assessment_ctx = self._assessment_context(meta, rows)
            if assessment_ctx is None:
                raise ConflictError(
                    "worked-example selection context unavailable")
            assessment_ctx[worked_example_selection.CONTEXT_KEY] = cloned
            practice = [copy.deepcopy(c["context"])
                        for c in end.get("practice_contexts") or []]
            # The deterministic rebuild only supplies the replacement
            # card and the updated frozen selection context; the stored
            # narrative, other example cards and practice are kept.
            rebuilt = run_multi_feedback(
                evidence, selection=selection,
                practice_contexts=practice,
                assessment_context=assessment_ctx)
            now = utc_now()
            target = next(
                (c for c in rebuilt.get("review_examples") or []
                 if c.get("candidate_id") == candidate_id), None)
            if target is None:
                raise ConflictError(
                    "rebuilt worked example card unavailable")
            review = copy.deepcopy(end["student_review"])
            cards = review.get("review_examples") or []
            position = next(
                (i for i, c in enumerate(cards)
                 if c.get("candidate_id") == candidate_id), None)
            if position is None:
                raise ValueError(
                    "candidate card not found in stored review")
            new_card = copy.deepcopy(target)
            new_card["explanation_source"] = "application"
            cards[position] = new_card
            sources = dict(
                review["trace"].get("worked_review_sources") or {})
            # Drop the replaced question's source entry when no other
            # current card still uses that question.
            old_question = (card or {}).get("question_id")
            remaining = {c.get("question_id") for c in cards}
            if old_question and old_question not in remaining:
                sources.pop(old_question, None)
            sources[question_id] = "application"
            review["trace"]["worked_review_sources"] = sources
            review["trace"]["worked_example_override"] = {
                "candidate_id": candidate_id,
                "question_id": question_id,
                "reviewer_label": reviewer,
                "overridden_at": now}
            audit = {
                "applied_at": now, "candidate_id": candidate_id,
                "question_id": question_id,
                "reviewer_label": reviewer,
                "previous_preview_sha256": current,
                "previous_student_review": copy.deepcopy(
                    end.get("student_review")),
                "previous_feedback_edit": copy.deepcopy(
                    meta.get("feedback_edit")),
                "previous_provider_job": copy.deepcopy(
                    meta["provider_job"])}
            meta.setdefault(
                "worked_example_selection_history", []).append(audit)
            end[worked_example_selection.CONTEXT_KEY] = cloned
            end["student_review"] = review
            # The comparison baseline shows the same replacement card;
            # its unrelated narrative and cards are preserved.
            baseline = end.get("baseline_student")
            if isinstance(baseline, dict):
                for i, b_card in enumerate(
                        baseline.get("review_examples") or []):
                    if b_card.get("candidate_id") == candidate_id:
                        baseline["review_examples"][i] = copy.deepcopy(
                            new_card)
            # The aggregate provider_job keeps its original narrative
            # provenance untouched; only the scoped worked_example_job
            # records this replacement's execution.
            self._refresh_saved_edit_card(
                meta, session, candidate_id)
            job = self._admit_worked_example_job(
                session_id, meta, new_card, candidate_id, question_id,
                now)
            self._save_meta(meta)
            if job["status"] == "pending":
                try:
                    self.provider_pool.submit(
                        self._run_worked_example_job,
                        session_id, job["job_id"])
                except RuntimeError:
                    with self.lock:
                        self._pending_provider = max(
                            0, self._pending_provider - 1)
                        meta = self._load_meta(session_id)
                        current_job = (
                            meta["checkpoints"]["end"]
                            .get("worked_example_job") or {})
                        if current_job.get("job_id") == job["job_id"]:
                            current_job.update({
                                "status": "fallback",
                                "reason": "service_closing",
                                "finished_at": utc_now()})
                            meta["checkpoints"]["end"][
                                "worked_example_job"] = current_job
                            self._save_meta(meta)
                            job = current_job
            return {"saved": True,
                    "preview_sha256": canonical_digest(
                        self._final_preview(meta)),
                    "worked_example_job": copy.deepcopy(job)}

    def _refresh_saved_edit_card(self, meta, session, candidate_id):
        """Patch only the replaced worked-example card inside a saved
        structured edit preview, preserving the teacher's unrelated
        sections, content and other cards. Callers hold the lock."""
        edit = meta.get("feedback_edit")
        if not isinstance(edit, dict):
            return
        preview = edit.get("preview")
        if not isinstance(preview, dict):
            return
        fresh = self._multi_preview(meta, session)
        fresh_card = next(
            (c for c in fresh.get("review_examples") or []
             if c.get("candidate_id") == candidate_id), None)
        if fresh_card is None:
            return
        for i, card in enumerate(
                preview.get("review_examples") or []):
            if card.get("candidate_id") == candidate_id:
                preview["review_examples"][i] = copy.deepcopy(
                    fresh_card)
        if (preview.get("review_example") or {}).get(
                "candidate_id") == candidate_id:
            preview["review_example"] = copy.deepcopy(fresh_card)

    def _admit_worked_example_job(self, session_id, meta, card,
                                  candidate_id, question_id, now):
        """Queue the scoped worked-example generation job for one
        already-saved checked card. Returns the job record; a pending
        job has reserved a provider pool slot the caller must submit or
        release. Callers hold the lock."""
        end = meta["checkpoints"]["end"]
        job = {"job_id": secrets.token_hex(8), "status": "unavailable",
               "candidate_id": candidate_id, "question_id": question_id,
               "queued_at": now, "finished_at": now}
        hosted = (meta["provider_job"].get("provider_mode") == "hosted")
        adapter = self.worked_example_generator
        if card.get("status") != "ready":
            job["reason"] = "no_checked_solution"
        elif not hosted:
            job.update({"status": "ready", "source": "application",
                        "reason": "rules_mode_application_only"})
        elif adapter is None:
            job.update({"status": "fallback",
                        "reason": "adapter_unavailable"})
        elif self._provider_session_event(session_id, meta).is_set():
            job.update({"status": "cancelled",
                        "reason": "replay_case_failed"})
        elif self._closing:
            job.update({"status": "fallback",
                        "reason": "service_closing"})
        elif self._pending_provider >= self.max_pending_jobs:
            job.update({"status": "fallback",
                        "reason": "demo_provider_queue_capacity"})
        else:
            job.update({"status": "pending", "reason": None,
                        "finished_at": None})
            self._pending_provider += 1
        end["worked_example_job"] = job
        return job

    def _run_worked_example_job(self, session_id, job_id):
        """Scoped worker: regenerates wording for exactly one selected
        worked-example card, overlaying only that card on success."""
        adapter = self.worked_example_generator
        reply = None
        execution = None
        failure = None
        try:
            event = self._provider_session_event(session_id)
            try:
                if adapter is None:
                    failure = "adapter_unavailable"
                elif event.is_set():
                    return
                else:
                    with self._provider_session_scope(session_id):
                        with self._adapter_scope(adapter):
                            if event.is_set():
                                return
                            if hasattr(adapter, "execution"):
                                adapter.execution = None
                            self._reset_rejected_draft(adapter)
                            with self.lock:
                                meta = self._load_meta(session_id)
                                end = meta["checkpoints"]["end"]
                                job = (end.get("worked_example_job")
                                       or {})
                                card = None
                                if (job.get("job_id") == job_id
                                        and job.get("status")
                                        == "pending"):
                                    card = next(
                                        (c for c in (
                                            end["student_review"]
                                            .get("review_examples")
                                            or [])
                                         if c.get("candidate_id")
                                         == job.get("candidate_id")),
                                        None)
                                if (card is None
                                        or card.get("question_id")
                                        != job.get("question_id")):
                                    failure = "worked_card_unavailable"
                                    card = None
                                else:
                                    try:
                                        card = (
                                            build_worked_example_input(
                                                card))
                                    except ValueError:
                                        failure = (
                                            "worked_card_unavailable")
                                        card = None
                            if card is not None:
                                try:
                                    reply = adapter.generate(card)
                                finally:
                                    # Capture the execution record and
                                    # sanitized last_metadata inside the
                                    # adapter scope even when generate
                                    # raised, so retry/failure evidence
                                    # and rejected drafts are retained.
                                    execution = copy.deepcopy(
                                        getattr(adapter, "execution",
                                                None))
                                    if execution is None:
                                        execution = (
                                            self._midpoint_exec_record(
                                                adapter))
            except SystemExit:
                failure = "demo_call_budget_exhausted"
            except Exception:
                failure = "provider_error"
            if event.is_set():
                return
            with self.lock:
                try:
                    meta = self._load_meta(session_id)
                except FileNotFoundError:
                    return
                if event.is_set():
                    return
                end = meta["checkpoints"]["end"]
                job = end.get("worked_example_job") or {}
                if (job.get("job_id") != job_id
                        or job.get("status") != "pending"):
                    # A newer override or a cancellation superseded
                    # this run; its late reply is discarded.
                    return
                review = end.get("student_review")
                card = next(
                    (c for c in (review or {}).get("review_examples")
                     or [] if c.get("candidate_id")
                     == job.get("candidate_id")), None)
                finished = utc_now()
                if (meta.get("feedback_release")
                        or card is None
                        or card.get("question_id")
                        != job.get("question_id")):
                    job.update({"status": "fallback",
                                "reason": "superseded_draft",
                                "finished_at": finished})
                else:
                    overlay = None
                    if failure is None and reply is not None:
                        try:
                            overlay = validate_worked_example_reply(
                                reply, card)
                        except Exception:
                            overlay = None
                    if overlay is None:
                        job.update({
                            "status": "fallback",
                            "reason": failure or "invalid_generation",
                            "finished_at": finished,
                            "execution": execution})
                    else:
                        _overlay_worked_review(card, overlay)
                        sources = dict(
                            review["trace"].get(
                                "worked_review_sources") or {})
                        sources[job["question_id"]] = "provider"
                        review["trace"]["worked_review_sources"] = (
                            sources)
                        session = self._context_for(meta)[
                            "mcqs"].private_record(session_id)
                        self._refresh_saved_edit_card(
                            meta, session, job["candidate_id"])
                        job.update({"status": "ready",
                                    "finished_at": finished,
                                    "execution": execution})
                end["worked_example_job"] = job
                self._save_meta(meta)
        finally:
            with self.lock:
                self._pending_provider = max(
                    0, self._pending_provider - 1)

    def save_feedback_edit(self, session_id, body):
        if (not isinstance(body, dict)
                or not set(body) <= {
                    "message_sha256", "sections", "content",
                    "document_text", "document_blocks",
                    "reviewer_label"}
                or "message_sha256" not in body):
            raise ValueError("feedback edit body has unexpected fields")
        has_document = "document_text" in body
        if has_document and ("sections" in body or "content" in body):
            raise ValueError(
                "document_text cannot be combined with sections or "
                "content")
        if not has_document and "sections" not in body:
            raise ValueError("feedback edit body has unexpected fields")
        if "document_blocks" in body and not has_document:
            raise ValueError(
                "document_blocks requires document_text")
        digest = body["message_sha256"]
        if (not isinstance(digest, str)
                or len(digest) != 64 or any(
                    ch not in "0123456789abcdef" for ch in digest)):
            raise ValueError("message_sha256 must be a sha256 hex digest")
        document = None
        blocks = None
        texts = {}
        if has_document:
            document = _strict_text(
                body["document_text"], "document_text", 32768)
            if not document.strip():
                raise ValueError("document_text must be nonempty")
            if "document_blocks" in body:
                blocks = _validated_document_blocks(
                    body["document_blocks"])
                if _document_blocks_text(blocks) != document:
                    raise ValueError(
                        "document_text must equal the canonical "
                        "serialization of document_blocks")
        else:
            submitted = body["sections"]
            if (not isinstance(submitted, dict)
                    or set(submitted) != set(FULL_FEEDBACK_SECTION_KEYS)):
                raise ValueError(
                    "sections must contain exactly assessment_summary, "
                    "observed_strengths, review_focus, next_steps")
            for key in FULL_FEEDBACK_SECTION_KEYS:
                text = _strict_text(
                    submitted[key], f"sections.{key}", 2000)
                if not text.strip():
                    raise ValueError(f"sections.{key} must be nonempty")
                texts[key] = text
        content = body.get("content")
        if content is None and "content" in body:
            raise ValueError("content must not be null")
        if content is not None and not isinstance(content, dict):
            raise ValueError("content must be an object")
        reviewer = _strict_text(
            body.get("reviewer_label", "anonymous"), "reviewer_label", 80)
        with self.lock:
            meta = self._load_meta(session_id)
            session = self._context_for(meta)["mcqs"].private_record(
                session_id)
            if session["status"] != "complete":
                raise ValueError(
                    "feedback editing requires a completed session")
            if self._worked_example_job_pending(meta):
                raise ConflictError(
                    "a worked example update is still in progress")
            preview = self._final_preview(meta)
            if preview is None:
                raise ConflictError("feedback is not ready for editing")
            current = canonical_digest(preview)
            if current != digest:
                raise ConflictError(
                    "stale feedback revision", current=current)
            titles = dict(zip(FULL_FEEDBACK_SECTION_KEYS,
                              FULL_FEEDBACK_SECTION_TITLES))
            for section in preview["sections"]:
                if (section.get("kind") in titles
                        and section.get("title")):
                    titles[section["kind"]] = section["title"]
            edited = copy.deepcopy(preview)
            if document is not None:
                edited["document_text"] = document
                edited["document_edited"] = True
                edited["text"] = document
                if blocks is not None:
                    edited["document_blocks"] = copy.deepcopy(blocks)
                else:
                    edited.pop("document_blocks", None)
            else:
                edited.pop("document_text", None)
                edited.pop("document_edited", None)
                edited.pop("document_blocks", None)
                edited["sections"] = [
                    {"kind": kind, "title": titles[kind],
                     "text": texts[kind]}
                    for kind in FULL_FEEDBACK_SECTION_KEYS]
                edited["text"] = "\n\n".join(
                    s["text"] for s in edited["sections"] if s["text"])
                if content is not None:
                    self._apply_feedback_content(edited, content)
            prior = meta.get("feedback_edit")
            meta["feedback_edit"] = {
                "preview": copy.deepcopy(edited),
                "source_preview_sha256": current,
                "source_preview": copy.deepcopy(
                    prior["source_preview"]
                    if prior is not None
                    and prior.get("source_preview") is not None
                    else preview),
                "saved_at": utc_now(),
                "reviewer_label": reviewer}
            self._save_meta(meta)
            return {"saved": True,
                    "preview_sha256": canonical_digest(edited),
                    "saved_at": meta["feedback_edit"]["saved_at"]}

    @staticmethod
    def _edit_text(value, field, nonempty=True):
        text = _strict_text(value, field, EDIT_MAX_TEXT)
        if nonempty and not text.strip():
            raise ValueError(f"{field} must be nonempty")
        return text

    @classmethod
    def _edit_text_list(cls, value, field, count=None, minimum=0):
        if (not isinstance(value, list) or len(value) > EDIT_MAX_LIST
                or len(value) < minimum
                or (count is not None and len(value) != count)):
            raise ValueError(f"{field} has an invalid length")
        return [cls._edit_text(item, f"{field}.{index}")
                for index, item in enumerate(value)]

    @staticmethod
    def _mark_teacher_edited(card, before):
        card["teacher_edited"] = True
        if "original_content" not in card:
            card["original_content"] = {
                key: value for key, value in before.items()
                if key not in ("teacher_edited", "original_content")}

    @classmethod
    def _patch_review_card(cls, card, patch, field):
        if (not isinstance(patch, dict)
                or not set(patch) <= EDIT_REVIEW_FIELDS):
            raise ValueError(f"{field} has unexpected fields")
        for key, value in patch.items():
            name = f"{field}.{key}"
            if key not in card:
                raise ValueError(f"{name} is not present in the card")
            if key in ("title", "for_subtopic_name", "prompt",
                       "selected_answer", "correct_answer",
                       "reasoning_question", "socratic_prompt"):
                card[key] = cls._edit_text(value, name)
            elif key == "options":
                card[key] = cls._edit_text_list(
                    value, name, count=len(card["options"]))
            elif key == "steps":
                card[key] = cls._edit_text_list(
                    value, name,
                    minimum=1 if card.get("status") == "ready" else 0)
                if "explanation" not in patch and "explanation" in card:
                    card["explanation"] = "\n".join(card[key])
            elif key == "progressive_hints":
                card[key] = cls._edit_text_list(
                    value, name,
                    count=len(card["progressive_hints"]))
            else:
                card[key] = cls._edit_text(value, name, nonempty=False)

    @classmethod
    def _patch_practice_card(cls, card, patch, field):
        if (not isinstance(patch, dict)
                or not set(patch) <= EDIT_PRACTICE_FIELDS):
            raise ValueError(f"{field} has unexpected fields")
        for key, value in patch.items():
            name = f"{field}.{key}"
            if key not in card:
                raise ValueError(f"{name} is not present in the card")
            if key in ("topic_label", "message", "instruction"):
                card[key] = cls._edit_text(value, name)
            elif key == "notice":
                if value is None:
                    if card["notice"] is not None:
                        raise ValueError(f"{name} must be a string")
                    card["notice"] = None
                else:
                    card["notice"] = cls._edit_text(
                        value, name, nonempty=False)
            elif key == "question":
                question = card["question"]
                if (not isinstance(question, dict)
                        or not isinstance(value, dict)
                        or not set(value) <= EDIT_QUESTION_FIELDS):
                    raise ValueError(
                        f"{name} has unexpected fields")
                for qkey, qvalue in value.items():
                    qname = f"{name}.{qkey}"
                    if qkey not in question:
                        raise ValueError(
                            f"{qname} is not present in the card")
                    if qkey == "options":
                        question["options"] = cls._edit_text_list(
                            qvalue, qname,
                            count=len(question["options"]))
                    else:
                        question[qkey] = cls._edit_text(qvalue, qname)
            else:
                relationship = card["relationship"]
                if (not isinstance(relationship, dict)
                        or not isinstance(value, dict)
                        or not set(value) <= {"message"}):
                    raise ValueError(
                        f"{name} may only patch message")
                if "message" in value:
                    card["relationship"]["message"] = cls._edit_text(
                        value["message"], f"{name}.message")

    @classmethod
    def _apply_feedback_content(cls, preview, content):
        if not set(content) <= EDIT_CONTENT_KEYS:
            raise ValueError("content has unexpected fields")
        for key, value in content.items():
            if value is None:
                raise ValueError(f"content.{key} must not be null")
        titles = content.get("section_titles")
        if titles is not None:
            if (not isinstance(titles, dict) or not titles
                    or not set(titles) <= set(FULL_FEEDBACK_SECTION_KEYS)):
                raise ValueError(
                    "content.section_titles must use the four "
                    "section keys")
            for section in preview["sections"]:
                if section["kind"] in titles:
                    section["title"] = cls._edit_text(
                        titles[section["kind"]],
                        f"content.section_titles.{section['kind']}")
        labels = content.get("skill_labels")
        if labels is not None:
            skills = preview.get("skills") or []
            if (not isinstance(labels, list)
                    or len(labels) != len(skills)
                    or len(labels) > EDIT_MAX_LIST):
                raise ValueError(
                    "content.skill_labels must supply one label per "
                    "preview skill")
            for index, (skill, label) in enumerate(zip(skills, labels)):
                text = cls._edit_text(
                    label, f"content.skill_labels.{index}")
                if text != skill["skill_name"]:
                    skill["skill_name"] = text
                    skill["teacher_edited"] = True
        if "worked_review_notice" in content:
            if "worked_review_notice" not in preview:
                raise ValueError(
                    "content.worked_review_notice is not present in "
                    "the preview")
            preview["worked_review_notice"] = cls._edit_text(
                content["worked_review_notice"],
                "content.worked_review_notice", nonempty=False)
        review_cards = preview.get("review_examples")
        if review_cards is None:
            single = preview.get("review_example")
            review_cards = [single] if single is not None else None
        review_patches = content.get("review_examples")
        if review_patches is not None:
            if (review_cards is None
                    or not isinstance(review_patches, list)
                    or len(review_patches) != len(review_cards)
                    or len(review_patches) > EDIT_MAX_LIST):
                raise ValueError(
                    "content.review_examples must supply one patch per "
                    "preview review card")
            for index, (card, patch) in enumerate(
                    zip(review_cards, review_patches)):
                before = copy.deepcopy(card)
                cls._patch_review_card(
                    card, patch, f"content.review_examples.{index}")
                if card != before:
                    cls._mark_teacher_edited(card, before)
        if (preview.get("review_examples")
                and "review_example" in preview):
            preview["review_example"] = preview["review_examples"][0]
        practice_cards = preview.get("practice_activities")
        if practice_cards is None:
            single = preview.get("practice")
            practice_cards = [single] if single is not None else None
        practice_patches = content.get("practice_activities")
        if practice_patches is not None:
            if (practice_cards is None
                    or not isinstance(practice_patches, list)
                    or len(practice_patches) != len(practice_cards)
                    or len(practice_patches) > EDIT_MAX_LIST):
                raise ValueError(
                    "content.practice_activities must supply one patch "
                    "per preview practice card")
            for index, (card, patch) in enumerate(
                    zip(practice_cards, practice_patches)):
                before = copy.deepcopy(card)
                cls._patch_practice_card(
                    card, patch, f"content.practice_activities.{index}")
                if card != before:
                    cls._mark_teacher_edited(card, before)
        if (preview.get("practice_activities")
                and "practice" in preview):
            preview["practice"] = preview["practice_activities"][0]

    # ---------------- simulation ----------------

    def simulate(self, profile, seed, bank_mode="demo"):
        if profile not in SIMULATION_PROFILES:
            raise ValueError("profile is not whitelisted")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        context = self._bank_context(bank_mode)
        simulation_bank = context["bank"]
        if bank_mode != "demo" and profile == "weak_fractions_only":
            # The historical scenario generator names the demo fraction topic.
            # Adapt its display label locally; source bank records stay unchanged.
            simulation_bank = {**simulation_bank, "skill_names": {
                sid: ("Murtoluvut" if name == "Murtolukujen kerto- ja jakolasku" else name)
                for sid, name in simulation_bank["skill_names"].items()}}
        rows = generate_responses(
            simulation_bank, {"profile": profile, "seed": seed,
                        "name": f"demo_{profile}_{seed}"})
        created = self.create_session(
            bank_mode=bank_mode,
            label=f"simulated:{profile} seed={seed}",
            simulated={"profile": profile, "seed": seed,
                       "label": "simulated response pattern, "
                                "not a diagnosis"})
        session_id = created["session_id"]
        token = created["student_token"]
        for row in rows:
            self.submit_response(session_id, token, {
                "question_id": row["question_id"],
                "selected_index": row["selected_index"]})
        return {"session_id": session_id, "student_token": token,
                "student_url":
                    f"/?session={session_id}#token={token}",
                "snapshot": self.snapshot(session_id, token)}
