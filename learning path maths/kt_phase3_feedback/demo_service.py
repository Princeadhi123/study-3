"""Local synthetic demo service: sessions, jobs, diagnostics, reviews.

This is a localhost research demo over the approved private bank. It is
not the live student API: every session is attested synthetic, all
feedback remains a draft requiring educator review, and KT
values stay private research diagnostics that never reach student
payloads.

Provider calls are optional, bounded, and never retried automatically.
The single provider worker owns the Cached* adapters so provider state
is shared by exactly one thread; the diagnostics worker owns the frozen
model. Neither pool blocks student submissions.
"""
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
from aitta_generator import AittaGenerator
from active_payload import active_payload
from evidence_feedback import (
    build_evidence, build_research_evidence, canonical_digest, run_feedback)
from evidence_feedback_policy import (
    POLICY_VERSION, SELECTION_INSTRUCTIONS, SELECTION_PROMPT_VERSION)
from evidence_providers import EvidenceJevSelector
from feedback_service import assessment_feedback_graph
from integrated_synthetic_pipeline import (
    CachedReviewOpening, CachedReviewSelector)
from jev_selector import _read_env_file
from live_diagnostics import LiveDiagnostics
from mcq_service import HALF_LENGTH, FULL_LENGTH, MCQSessionService
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
from synthetic_feedback import GENERATION_INSTRUCTIONS, PROMPT_VERSION
from transport_diagnostics import InstrumentedEvidenceAittaGenerator

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


class DemoService:
    """Owns demo sessions, async provider/diagnostics jobs, and reviews."""

    def __init__(self, root=DEFAULT_ROOT, bank=None, taxonomy=None,
                 provider_mode="rules", jev_env_file=None, call_budget=12,
                 provider_timeout=120.0, diagnostics=None,
                 selector=None, generator=None, max_pending_jobs=16,
                 replay_report=DEFAULT_REPORT, replay_source=DEFAULT_SOURCE,
                 practice_pool=None, practice_target_band=None,
                 recommender=None):
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
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
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
        self.meta_dir = self.root / "metadata"
        self.meta_dir.mkdir(parents=True, exist_ok=True)
        self.provider_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="demo-provider")
        self.diag_pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="demo-diagnostics")
        self.max_pending_jobs = max_pending_jobs
        self._pending_provider = 0
        self._pending_diag = 0
        self.diagnostics = (diagnostics if diagnostics is not None
                            else LiveDiagnostics())
        if provider_mode not in ("rules", "hosted"):
            raise ValueError("provider_mode must be rules or hosted")
        self.provider_mode = provider_mode
        self.call_budget = call_budget
        self.provider_timeout = provider_timeout
        self.cache = None
        if selector is not None or generator is not None:
            # Injected adapters (tests or pre-wired hosted providers).
            self.selector, self.generator = selector, generator
        elif provider_mode == "hosted":
            self.selector, self.generator = self._hosted_adapters(
                jev_env_file, call_budget, provider_timeout)
        else:
            self.selector, self.generator = None, None
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

    @staticmethod
    def available_banks():
        return [{"bank_mode": mode, "label": label, "question_count": FULL_LENGTH,
                 "research_pending_review": mode != "demo"}
                for mode, label in BANK_LABELS.items()]

    def _hosted_adapters(self, jev_env_file, call_budget, timeout):
        """Construct real providers once for the single provider worker."""
        if jev_env_file is None:
            raise ValueError("hosted mode requires an external Jev key file")
        native_selector = EvidenceJevSelector(
            _read_env_file(Path(jev_env_file)), timeout=timeout)
        inner = AittaGenerator.from_env()
        inner._timeout = timeout
        native_generator = InstrumentedEvidenceAittaGenerator(inner)
        captures = self.root / "provider_captures"
        captures.mkdir(exist_ok=True)
        self.cache = CaptureCache(captures, call_budget)
        endpoint_hash = hashlib.sha256(
            inner._endpoint.encode("utf-8")).hexdigest()
        return (CachedReviewSelector(native_selector, self.cache),
                CachedReviewOpening(native_generator, self.cache,
                                    inner._model, endpoint_hash))

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
                    job = checkpoint.get("diagnostics_job")
                    if job and job["status"] == "pending":
                        job["status"] = "unavailable"
                        job["reason"] = "interrupted_by_restart"
                        checkpoint["diagnostics"] = {
                            "status": "unavailable",
                            "reason": "interrupted_by_restart"}
                        changed = True
                    rjob = checkpoint.get("recommendation_job")
                    if rjob and rjob["status"] == "pending":
                        rjob["status"] = "unavailable"
                        rjob["reason"] = "interrupted_by_restart"
                        checkpoint["recommendation"] = {
                            "status": "unavailable",
                            "mode": "shadow_only",
                            "reason": "interrupted_by_restart",
                            "used_for_student_advice": False,
                            "used_for_feedback": False}
                        changed = True
                    diag = checkpoint.get("diagnostics")
                    if (isinstance(diag, dict)
                            and diag.get("status") == "ready"
                            and (diag.get("checkpoint") != name
                                 or diag.get("answer_count")
                                 != CHECKPOINT_LENGTHS[name])):
                        # Recorded against the wrong checkpoint; never
                        # redrive it, just refuse to present it.
                        checkpoint["diagnostics"] = {
                            "status": "unavailable",
                            "reason": "checkpoint_alignment_mismatch",
                            "recorded_checkpoint": diag.get("checkpoint"),
                            "recorded_answer_count":
                                diag.get("answer_count")}
                        checkpoint["diagnostics_job"] = {
                            "status": "unavailable",
                            "reason": "checkpoint_alignment_mismatch",
                            "finished_at": utc_now()}
                        changed = True
                job = meta.get("provider_job")
                if job and job["status"] == "pending":
                    job["status"] = "fallback"
                    job["interrupted"] = True
                    self._publish_baselines(meta)
                    changed = True
                if changed:
                    self._save_meta(meta)

    def close(self, wait=False):
        self.replays.close(wait=True)
        self.provider_pool.shutdown(wait=wait)
        self.diag_pool.shutdown(wait=wait)

    # ---------------- metadata persistence ----------------

    def _meta_path(self, session_id):
        return self.meta_dir / f"{session_id}.json"

    def _save_meta(self, meta):
        """Atomic metadata write with a unique temp file, under lock."""
        target = self._meta_path(meta["session_id"])
        tmp = target.with_name(
            f"{target.name}.{secrets.token_hex(6)}.tmp")
        with tmp.open("w", encoding="utf-8") as stream:
            json.dump(meta, stream, ensure_ascii=False, indent=2)
        tmp.replace(target)

    def _load_meta(self, session_id):
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

    def _new_meta(self, session_id, token, label=None, simulated=None, bank_mode="demo"):
        context = self._bank_context(bank_mode)
        return {
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
                    "baseline_student": None, "graph": None,
                    "diagnostics": None,
                    "diagnostics_job": {"status": "not_requested"}},
                "end": {
                    "evidence": None, "baseline_student": None,
                    "baseline_teacher": None, "student_review": None,
                    "teacher_review": None, "provider_job": None,
                    "graph": None, "diagnostics": None,
                    "diagnostics_job": {"status": "not_requested"},
                    "recommendation": None,
                    "recommendation_job": {"status": "not_requested"}}},
            "reviews": []}

    def create_session(self, label=None, simulated=None, replay=None, provider_mode=None,
                       bank_mode="demo"):
        if provider_mode not in (None, "rules", self.provider_mode):
            raise ValueError("provider mode is not configured")
        token = secrets.token_urlsafe(32)
        with self.lock:
            if replay and bank_mode != "demo":
                raise ValueError("historical replays require the demo bank")
            context = self._bank_context(bank_mode)
            started = context["mcqs"].start_session()
            session_id = started["session_id"]
            meta = self._new_meta(
                session_id, token, label=label, simulated=simulated, bank_mode=bank_mode)
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
        review = None
        if checkpoint == "end":
            job = meta["provider_job"]
            # A stored student result is safe to show even when the
            # aggregate job fell back because the teacher leg failed.
            if (job["status"] in ("ready", "fallback")
                    and block.get("student_review") is not None):
                review = block["student_review"]
            if review is None:
                review = block["baseline_student"]
        else:
            review = block["baseline_student"]
        if review is None:
            return None
        message = review["message"]
        if checkpoint == "midpoint":
            return {"sections": copy.deepcopy(message["sections"]),
                    "text": message["text"]}
        evidence = block.get("evidence") or {}
        return {
            "sections": copy.deepcopy(message["sections"]),
            "text": message["text"],
            "total": {"correct": evidence["total"]["correct"],
                      "out_of": evidence["total"]["out_of"]},
            "skills": [
                {"skill_id": row["skill_id"],
                 "skill_name": row["skill_name"],
                 "correct": row["correct"],
                 "out_of": row["out_of"]}
                for row in evidence["skills"]],
            "requires_educator_review": True}

    def _public_snapshot(self, session_id, meta=None):
        if meta is None:
            meta = self._load_meta(session_id)
        session = self._context_for(meta)["mcqs"].private_record(session_id)
        return {
            "session_id": session_id,
            "synthetic": True,
            "bank_mode": meta.get("bank_mode", "demo"),
            "bank_label": BANK_LABELS[meta.get("bank_mode", "demo")],
            "status": session["status"],
            "answered_count": len(session["responses"]),
            "current_question": self._current_question(session, meta),
            "feedback": {
                "midpoint": self._student_feedback(
                    session_id, meta, "midpoint"),
                "end": self._student_feedback(session_id, meta, "end")},
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
        block["graph"] = assessment_feedback_graph(
            context["bank"], context["taxonomy"], rows)
        self._admit_diagnostics(session_id, meta, "midpoint", rows)

    def _finish_end(self, session_id, meta):
        rows = copy.deepcopy(self._rows(session_id))
        context = self._context_for(meta)
        evidence = self._evidence(meta, rows)
        end = meta["checkpoints"]["end"]
        end["evidence"] = evidence
        end["baseline_student"] = run_feedback(
            evidence, "student", "end")
        end["baseline_teacher"] = run_feedback(
            evidence, "teacher", "end")
        end["graph"] = assessment_feedback_graph(
            context["bank"], context["taxonomy"], rows)
        mode = meta["provider_job"]["provider_mode"]
        if self._pending_provider >= self.max_pending_jobs:
            meta["provider_job"] = {
                "status": "fallback",
                "provider_mode": mode,
                "queued_at": utc_now(), "finished_at": utc_now(),
                "reason": "demo_provider_queue_capacity"}
            for audience in ("student", "teacher"):
                end[f"{audience}_review"] = self._baseline_review(
                    evidence, audience, "demo_provider_queue_capacity")
        else:
            self._pending_provider += 1
            meta["provider_job"] = {
                "status": "pending", "provider_mode": mode,
                "queued_at": utc_now()}
            self.provider_pool.submit(self._run_provider_job, session_id)
        end["provider_job"] = copy.deepcopy(meta["provider_job"])
        self._admit_diagnostics(session_id, meta, "end", rows)
        self._admit_recommendation(session_id, meta, rows)

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
                self._save_meta(meta)
        finally:
            with self.lock:
                self._pending_diag = max(0, self._pending_diag - 1)

    def _baseline_review(self, evidence, audience, reason):
        review = run_feedback(evidence, audience, "end")
        review = copy.deepcopy(review)
        review["trace"]["fallback_reason"] = reason
        return review

    def _audience_result(self, evidence, audience, provider_mode=None):
        """One audience through providers; isolated failures per audience."""
        executions = {"selector": None, "generator": None}
        if (provider_mode or self.provider_mode) == "rules":
            review = self._baseline_review(
                evidence, audience, None)
            review["trace"]["fallback_reason"] = None
            executions["selector"] = {"status": "rules_mode_no_providers"}
            executions["generator"] = {"status": "rules_mode_no_providers"}
            return review, executions
        try:
            if hasattr(self.selector, "execution"):
                self.selector.execution = None
            if hasattr(self.generator, "execution"):
                self.generator.execution = None
            review = run_feedback(evidence, audience, "end",
                                  self.selector, self.generator)
            executions["selector"] = copy.deepcopy(
                getattr(self.selector, "execution", None))
            executions["generator"] = copy.deepcopy(
                getattr(self.generator, "execution", None))
            if executions["selector"] is None:
                executions["selector"] = (
                    {"status": "local_single_candidate"}
                    if len(review["candidates"]) == 1
                    else {"status": "no_execution_record"})
            if executions["generator"] is None:
                if (review["trace"]["fallback_reason"]
                        in SELECTOR_FAILURE_REASONS):
                    executions["generator"] = {
                        "status": "skipped_selector_failure"}
                else:
                    executions["generator"] = {
                        "status": "no_execution_record"}
        except SystemExit:
            # Keep any completed stage record; only the stage that never
            # ran is marked budget_exhausted.
            review = self._baseline_review(
                evidence, audience, "demo_call_budget_exhausted")
            executions = {
                "selector": copy.deepcopy(
                    getattr(self.selector, "execution", None))
                or {"status": "budget_exhausted"},
                "generator": copy.deepcopy(
                    getattr(self.generator, "execution", None))
                or {"status": "budget_exhausted"}}
        except Exception:
            review = self._baseline_review(
                evidence, audience, "provider_error")
            executions = {
                "selector": copy.deepcopy(
                    getattr(self.selector, "execution", None))
                or {"status": "worker_error"},
                "generator": copy.deepcopy(
                    getattr(self.generator, "execution", None))
                or {"status": "worker_error"}}
        return review, executions

    def _run_provider_job(self, session_id):
        try:
            try:
                with self.lock:
                    rows = self._rows(session_id)
                    meta = self._load_meta(session_id)
                    mode = meta["provider_job"]["provider_mode"]
                evidence = self._evidence(meta, rows)
                results = {}
                for audience in ("student", "teacher"):
                    review, executions = self._audience_result(
                        evidence, audience, mode)
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
                end = meta["checkpoints"]["end"]
                if results is None:
                    evidence = self._evidence(meta, self._rows(session_id))
                    for audience in ("student", "teacher"):
                        end[f"{audience}_review"] = (
                            self._baseline_review(
                                evidence, audience, "provider_error"))
                    meta["provider_job"].update(
                        {"status": "fallback", "finished_at": finished,
                         "executions": {
                             a: {"status": "worker_error"}
                             for a in ("student", "teacher")}})
                else:
                    end["student_review"] = results["student"]["review"]
                    end["teacher_review"] = results["teacher"]["review"]
                    meta["provider_job"].update({
                        "status": job_status, "finished_at": finished,
                        "executions": {
                            audience: results[audience]["executions"]
                            for audience in results},
                        "prompt_versions": {
                            "selection": SELECTION_PROMPT_VERSION,
                            "generation": PROMPT_VERSION},
                        "policy_version": POLICY_VERSION})
                end["provider_job"] = copy.deepcopy(
                    meta["provider_job"])
                self._save_meta(meta)
        finally:
            with self.lock:
                self._pending_provider = max(
                    0, self._pending_provider - 1)

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
            if end.get("baseline_student") is None:
                end["baseline_student"] = run_feedback(
                    evidence, "student", "end")
            if end.get("baseline_teacher") is None:
                end["baseline_teacher"] = run_feedback(
                    evidence, "teacher", "end")
            for audience in ("student", "teacher"):
                if end.get(f"{audience}_review") is None:
                    end[f"{audience}_review"] = self._baseline_review(
                        evidence, audience, "interrupted_by_restart")
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
        return active_payload({
            "synthetic": True,
            "available_banks": self.available_banks(),
            "simulation_profiles": [{"id": name, **case_display(name)} for name in SIMULATION_PROFILES],
            "provider_mode": self.provider_mode,
            "call_budget": {"limit": self.call_budget,
                            "used": self.cache.new_calls
                            if self.cache is not None else 0},
            "provider_timeout_seconds": self.provider_timeout,
            "diagnostics": self.diagnostics.state,
            "shadow_practice": {"enabled": self.recommender is not None,
                                "mode": "shadow_only"},
            "bank_sha256": bank_fingerprint(self.bank),
            "taxonomy_sha256": canonical_digest(self.taxonomy),
            "policy_version": POLICY_VERSION,
            "selection_instructions": SELECTION_INSTRUCTIONS,
            "generation_instructions": GENERATION_INSTRUCTIONS,
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
        end_packages = {p["audience"]: p for p in case["packages"] if p["checkpoint"] == "end"}
        evidence = end_packages["student"]["review"]["sanitized_evidence"]
        identity = case_display(case["name"], evidence)
        try:
            responses = self.replays.source_cases()[case_id]
            rows = self._question_records(responses, library["source"].get("taxonomy_sha256") == canonical_digest(self.taxonomy))
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
                          "status": "complete", "answered_count": 40,
                          "read_only": True, "source_mode": "retained_original",
                          "observed_total": copy.deepcopy(evidence["total"]),
                          "question_responses": rows, "response_notice": response_notice,
                          "checkpoints": checkpoints, "review_hashes": {}, "reviews": [],
                          "review_contract": self.teacher_config()["review_contract"],
                          "provider_job": {"status": "ready", "provider_mode": library["policy"].get("provider_mode", "recorded"),
                                           "executions": executions},
                          "provenance": {"report_sha256": library["report_sha256"], "source": library["source"],
                                         "policy": library["policy"], "mode": "retained_original_not_recomputed"}}
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
            provenance = {
                **copy.deepcopy(context["provenance"]),
                "bank_sha256": meta["bank_sha256"],
                "taxonomy_sha256": meta["taxonomy_sha256"],
                "policy_version": meta["policy_version"],
                "selection_prompt_version": SELECTION_PROMPT_VERSION,
                "generation_prompt_version": PROMPT_VERSION}
            diagnostics = end.get("diagnostics") or {}
            if (isinstance(diagnostics, dict)
                    and isinstance(diagnostics.get("kt"), dict)):
                provenance["diagnostics"] = copy.deepcopy(
                    diagnostics["kt"].get("provenance", {}))
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
                "provenance": provenance})

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
