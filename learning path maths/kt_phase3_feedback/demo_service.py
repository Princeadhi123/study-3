"""Local synthetic demo service: sessions, jobs, diagnostics, reviews.

This is a localhost research demo over the approved private bank. It is
not the live student API: every session is attested synthetic, all
feedback remains a draft requiring educator review, and KT/conformal
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
from evidence_feedback import build_evidence, canonical_digest, run_feedback
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
from scenario_runner import generate_responses
from schemas import SESSION_SCHEMA
from session_store import SessionStore, bank_fingerprint, utc_now
from synthetic_feedback import GENERATION_INSTRUCTIONS, PROMPT_VERSION
from transport_diagnostics import InstrumentedEvidenceAittaGenerator

DEFAULT_ROOT = phase3_paths.ARTIFACTS / "live_demo_20261002"
SESSION_ID_RE = re.compile(r"\A[a-f0-9]{32}\Z")
META_SCHEMA = "phase3_live_demo_session_meta_v1"
CHECKPOINT_LENGTHS = {"midpoint": HALF_LENGTH, "end": FULL_LENGTH}
SELECTOR_FAILURE_REASONS = frozenset({
    "selector_error", "mutated_selection_payload", "invalid_selection"})
SIMULATION_PROFILES = (
    "all_correct", "all_incorrect", "weak_fractions_only",
    "first_half_correct_second_half_wrong", "alternating")
PUBLIC_QUESTION_KEYS = (
    "question_id", "skill_id", "text", "options")


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
                 selector=None, generator=None, max_pending_jobs=16):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        if bank is None:
            bank = json.loads(phase3_paths.require(
                phase3_paths.APPROVED_BANK).read_text(encoding="utf-8"))
        if taxonomy is None:
            taxonomy = json.loads(phase3_paths.require(
                phase3_paths.ASSESSMENT_TAXONOMY).read_text(encoding="utf-8"))
        self.bank, self.taxonomy = bank, taxonomy
        self.store = SessionStore(self.root / "sessions")
        self.mcqs = MCQSessionService(bank, self.store, taxonomy=taxonomy)
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
        self._recover_interrupted()
        # Lazy frozen-model prewarm; serialized on the diagnostics worker.
        self.diag_pool.submit(self.diagnostics.warmup)

    # ---------------- lifecycle ----------------

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
        if (meta.get("session_id") != session_id
                or meta.get("schema") != META_SCHEMA
                or meta.get("synthetic") is not True
                or meta.get("bank_sha256") != bank_fingerprint(self.bank)
                or meta.get("taxonomy_sha256")
                != canonical_digest(self.taxonomy)
                or meta.get("policy_version") != POLICY_VERSION):
            raise ValueError("metadata is incompatible")
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

    def _new_meta(self, session_id, token, label=None, simulated=None):
        return {
            "session_id": session_id,
            "schema": META_SCHEMA,
            "synthetic": True,
            "attestation": "synthetic_demo_not_real_learner_data",
            "label": label or f"demo-{session_id[:8]}",
            "simulated": simulated,
            "created_at": utc_now(),
            "bank_sha256": bank_fingerprint(self.bank),
            "taxonomy_sha256": canonical_digest(self.taxonomy),
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
                    "diagnostics_job": {"status": "not_requested"}}},
            "reviews": []}

    def create_session(self, label=None, simulated=None):
        token = secrets.token_urlsafe(32)
        with self.lock:
            started = self.mcqs.start_session()
            session_id = started["session_id"]
            meta = self._new_meta(
                session_id, token, label=label, simulated=simulated)
            self._save_meta(meta)
            snapshot = self._public_snapshot(session_id, meta)
        return {"session_id": session_id, "student_token": token,
                "snapshot": snapshot}

    def list_sessions(self):
        sessions = []
        with self.lock:
            for path in sorted(self.meta_dir.glob("*.json")):
                meta = self._try_load_meta(path.stem)
                if meta is None:
                    continue
                try:
                    session = self.mcqs.private_record(meta["session_id"])
                except Exception:
                    continue
                sessions.append({
                    "session_id": meta["session_id"],
                    "label": meta["label"],
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

    def _current_question(self, session):
        answered = len(session["responses"])
        if answered >= FULL_LENGTH or session["status"] == "complete":
            return None
        question = self.bank["questions"][answered]
        return {key: question[key] for key in PUBLIC_QUESTION_KEYS} | {
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
        session = self.mcqs.private_record(session_id)
        if meta is None:
            meta = self._load_meta(session_id)
        return {
            "session_id": session_id,
            "synthetic": True,
            "status": session["status"],
            "answered_count": len(session["responses"]),
            "current_question": self._current_question(session),
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

    def submit_response(self, session_id, token, row):
        with self.lock:
            meta = self._authorize(session_id, token)
            if (not isinstance(row, dict)
                    or set(row) != {"question_id", "selected_index"}
                    or not isinstance(row["question_id"], str)
                    or not isinstance(row["selected_index"], int)
                    or isinstance(row["selected_index"], bool)):
                raise ValueError(
                    "response must be {question_id, selected_index}")
            session = self.mcqs.private_record(session_id)
            responses = session["responses"]
            if (responses
                    and responses[-1]["question_id"] == row.get("question_id")):
                if responses[-1]["selected_index"] == row.get("selected_index"):
                    # Identical retry of the saved latest answer.
                    return self._public_snapshot(session_id, meta)
                raise ValueError("response does not match the required order")
            result = self.mcqs.submit_response(session_id, row)
            answered = result["position"]
            if answered == HALF_LENGTH:
                self._finish_midpoint(session_id, meta)
            elif answered == FULL_LENGTH:
                self._finish_end(session_id, meta)
            self._save_meta(meta)
            return self._public_snapshot(session_id, meta)

    def _rows(self, session_id):
        return self.mcqs.private_response_rows(session_id)

    def _finish_midpoint(self, session_id, meta):
        # Rows are snapshotted at admission: the queued worker must never
        # observe a later session state.
        rows = copy.deepcopy(self._rows(session_id))
        evidence = build_evidence(self.bank, self.taxonomy, rows)
        block = meta["checkpoints"]["midpoint"]
        block["baseline_student"] = run_feedback(
            evidence, "student", "midpoint")
        block["graph"] = assessment_feedback_graph(
            self.bank, self.taxonomy, rows)
        self._admit_diagnostics(session_id, meta, "midpoint", rows)

    def _finish_end(self, session_id, meta):
        rows = copy.deepcopy(self._rows(session_id))
        evidence = build_evidence(self.bank, self.taxonomy, rows)
        end = meta["checkpoints"]["end"]
        end["evidence"] = evidence
        end["baseline_student"] = run_feedback(
            evidence, "student", "end")
        end["baseline_teacher"] = run_feedback(
            evidence, "teacher", "end")
        end["graph"] = assessment_feedback_graph(
            self.bank, self.taxonomy, rows)
        if self._pending_provider >= self.max_pending_jobs:
            meta["provider_job"] = {
                "status": "fallback",
                "provider_mode": self.provider_mode,
                "queued_at": utc_now(), "finished_at": utc_now(),
                "reason": "demo_provider_queue_capacity"}
            for audience in ("student", "teacher"):
                end[f"{audience}_review"] = self._baseline_review(
                    evidence, audience, "demo_provider_queue_capacity")
        else:
            self._pending_provider += 1
            meta["provider_job"] = {
                "status": "pending", "provider_mode": self.provider_mode,
                "queued_at": utc_now()}
            self.provider_pool.submit(self._run_provider_job, session_id)
        end["provider_job"] = copy.deepcopy(meta["provider_job"])
        self._admit_diagnostics(session_id, meta, "end", rows)

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

    # ---------------- background jobs ----------------

    def _run_diagnostics(self, session_id, checkpoint, rows):
        try:
            result = self.diagnostics.evaluate(
                self.bank, self.taxonomy, rows)
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

    def _baseline_review(self, evidence, audience, reason):
        review = run_feedback(evidence, audience, "end")
        review = copy.deepcopy(review)
        review["trace"]["fallback_reason"] = reason
        return review

    def _audience_result(self, evidence, audience):
        """One audience through providers; isolated failures per audience."""
        executions = {"selector": None, "generator": None}
        if self.provider_mode == "rules":
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
                evidence = build_evidence(
                    self.bank, self.taxonomy, rows)
                results = {}
                for audience in ("student", "teacher"):
                    review, executions = self._audience_result(
                        evidence, audience)
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
                    evidence = build_evidence(
                        self.bank, self.taxonomy,
                        self._rows(session_id))
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
            session = self.mcqs.private_record(meta["session_id"])
            rows = [{"question_id": e["question_id"],
                     "selected_index": e["selected_index"]}
                    for e in session["responses"]]
            if len(rows) != FULL_LENGTH:
                return
            evidence = build_evidence(self.bank, self.taxonomy, rows)
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
            "bank_sha256": bank_fingerprint(self.bank)})

    def teacher_config(self):
        return {
            "synthetic": True,
            "provider_mode": self.provider_mode,
            "call_budget": {"limit": self.call_budget,
                            "used": self.cache.new_calls
                            if self.cache is not None else 0},
            "provider_timeout_seconds": self.provider_timeout,
            "diagnostics": self.diagnostics.state,
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
            "disclaimer": "local synthetic demo; not production auth"}

    def teacher_session(self, session_id):
        with self.lock:
            meta = self._load_meta(session_id)
            session = self.mcqs.private_record(session_id)
            questions = {q["question_id"]: q
                         for q in self.bank["questions"]}
            subtopics = {}
            for topic in self.taxonomy["topics"]:
                for sub in topic["subtopics"]:
                    for qid in sub["question_ids"]:
                        subtopics[qid] = (sub["id"], sub["name"])
            rows = []
            for event in session["responses"]:
                question = questions[event["question_id"]]
                sub_id, sub_name = subtopics[event["question_id"]]
                rows.append({
                    "position": event["position"],
                    "question_id": event["question_id"],
                    "skill_id": event["skill_id"],
                    "text": question["text"],
                    "options": question["options"],
                    "selected_index": event["selected_index"],
                    "answer_index": question["answer_index"],
                    "correct": event["correct"],
                    "answered_at": event["answered_at"],
                    "subtopic_id": sub_id,
                    "subtopic_name": sub_name})
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
            return {
                "session_id": session_id,
                "label": meta["label"],
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
                "provenance": provenance}

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
            session = self.mcqs.private_record(session_id)
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
            return {"recorded": True, "review": copy.deepcopy(entry)}

    # ---------------- simulation ----------------

    def simulate(self, profile, seed):
        if profile not in SIMULATION_PROFILES:
            raise ValueError("profile is not whitelisted")
        if isinstance(seed, bool) or not isinstance(seed, int):
            raise ValueError("seed must be an integer")
        rows = generate_responses(
            self.bank, {"profile": profile, "seed": seed,
                        "name": f"demo_{profile}_{seed}"})
        created = self.create_session(
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
