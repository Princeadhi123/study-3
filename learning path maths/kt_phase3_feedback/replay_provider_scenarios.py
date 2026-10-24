"""Replay frozen synthetic observed counts, never the bank, responses, or KT.

Identical provider requests share captured responses. This tests integration
coverage, not independent rerun stability, educational quality, or learning.
"""
import argparse
import contextlib
import contextvars
import copy
import hashlib
import json
import os
import random
import secrets
import threading
import time
from pathlib import Path

from aitta_generator import AittaGenerator, _validated_generation_payload
from jev_selector import (
    CRITERIA, MODEL, SELECTION_INSTRUCTIONS, SELECTION_PROMPT_VERSION,
    JevSelector, _validated_selection_payload)
from synthetic_feedback import (
    CANDIDATE_VERSION, GENERATION_INSTRUCTIONS, GENERATION_SCHEMA, INPUT_SCHEMA,
    POLICY_VERSION, PROMPT_VERSION, SELECTION_SCHEMA, _validate_input,
    run_synthetic_feedback)

SKILL_ALIASES = {
    "skill_5918fabc5b75": ("skill_a", "Arithmetic", "Peruslaskutoimitukset"),
    "skill_82e4c6d79fe0": ("skill_b", "Prices", "Hinta"),
    "skill_304b0fda845f": ("skill_c", "Fractions", "Murtoluvut"),
    "skill_91bcb30d3f0b": ("skill_d", "Percentages", "Prosenttilaskenta"),
}
ROLES = (("student", "midpoint"), ("student", "end"), ("teacher", "end"))


def digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


def _longpath(path):
    p = os.path.abspath(os.fspath(path))
    if os.name == "nt" and not p.startswith("\\\\?\\"):
        return "\\\\?\\" + p
    return p


def write_new(path, value):
    with open(_longpath(path), "x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=True)


def freeze(source):
    raw = source.read_bytes()
    original = json.loads(raw.decode("utf-8"))
    if (original.get("schema") != "phase3_fixed40_comparison_v1"
            or original.get("scope") !=
            "private_synthetic_fixed_40_cold_start_not_student_validation"):
        raise ValueError("only the saved synthetic fixed-40 artifact is allowed")
    scenarios = original["scenarios"]
    if len(scenarios) != 54 or len({s["name"] for s in scenarios}) != 54:
        raise ValueError("expected all 54 distinct scenario labels")
    order = [r["question_id"] for r in scenarios[0]["responses"]]
    cases = []
    for case in scenarios:
        rows = case["responses"]
        if (len(rows) != 40
                or [r["position"] for r in rows] != list(range(1, 41))
                or [r["question_id"] for r in rows] != order
                or any(type(r["correct"]) is not bool for r in rows)):
            raise ValueError("saved response contract differs across scenarios")
        observed = {}
        for checkpoint, limit in (("midpoint", 20), ("end", 40)):
            feed = case["observed"][checkpoint]
            skills = feed["skills"]
            if ([s["skill_id"] for s in skills] != list(SKILL_ALIASES)
                    or len(skills) != 4):
                raise ValueError("saved skill order does not match the explicit mapping")
            public = []
            for skill in skills:
                sid = skill["skill_id"]
                alias, name, original_name = SKILL_ALIASES[sid]
                matched = [r for r in rows[:limit] if r["skill_id"] == sid]
                if (skill["skill_name"] != original_name
                        or type(skill["correct"]) is not int
                        or type(skill["out_of"]) is not int
                        or (skill["correct"], skill["out_of"]) !=
                        (sum(r["correct"] for r in matched), len(matched))):
                    raise ValueError("saved observed counts disagree with response labels")
                public.append({"skill_id": alias, "skill_name": name,
                               "correct": skill["correct"], "out_of": skill["out_of"]})
            if checkpoint == "midpoint" and "total" in feed:
                raise ValueError("saved midpoint unexpectedly includes a total")
            if checkpoint == "end" and feed["total"] != {
                    "correct": sum(s["correct"] for s in public), "out_of": 40}:
                raise ValueError("saved end total disagrees with skill counts")
            _validate_input({"schema": INPUT_SCHEMA, "data_origin": "synthetic",
                             "audience": "student", "checkpoint": checkpoint,
                             "skills": public})
            observed[checkpoint] = public
        cases.append({"name": case["name"], "group": case["group"],
                      "profile": case["profile"], "seed": case["seed"],
                      "skills": observed})
    return {"schema": "phase3_frozen_provider_inputs_v1",
            "source_file": source.name, "source_sha256": hashlib.sha256(raw).hexdigest(),
            "scope": "synthetic_observed_counts_only",
            "validation": "counts_checked_against_saved_labels_not_private_answer_keys",
            "cases": cases}


def selection_request(payload):
    state = _validated_selection_payload(payload)
    return {"model": MODEL, "state": state, "questions": {"feedback_candidate": {
        "type": "choice", "instructions": SELECTION_INSTRUCTIONS,
        "criteria": {c["candidate_id"]: CRITERIA[c["candidate_id"]]
                     for c in state["candidates"]}}}}


def generation_request(payload, model):
    normalized = _validated_generation_payload(payload)
    selected = normalized["selected_candidate"]
    return {"model": model, "instructions": GENERATION_INSTRUCTIONS,
            "max_tokens": 1024, "reasoning_effort": "low",
            "response_format": {"type": "json_object"},
            "context": {"audience": normalized["audience"],
                        "checkpoint": normalized["checkpoint"],
                        "selected_candidate": {k: selected[k] for k in
                                               ("candidate_id", "strategy", "review_status")}}}


AUTOMATIC_RETRY_PROVIDERS = frozenset(
    {"aitta_full", "aitta_midpoint", "aitta_worked_example"})
AUTOMATIC_RETRY_MAX = 3
_RETRYABLE_STAGES = frozenset({"open_failed", "body_read_failed"})
_RETRYABLE_KINDS = frozenset({"timeout", "network_error"})
_RETRYABLE_HTTP = frozenset({429} | set(range(500, 600)))
_RETRY_AFTER_LIMIT_SECONDS = 60.0


def _automatic_retryable(execution):
    """Transient transport failure kinds only; never contract,
    credential, TLS, or content rejections."""
    transport = ((execution.get("metadata") or {})
                 .get("transport") or {})
    if transport.get("stage") not in _RETRYABLE_STAGES:
        return False
    kind = transport.get("failure_kind")
    if kind in _RETRYABLE_KINDS:
        return True
    return (kind == "http_error"
            and transport.get("http_status") in _RETRYABLE_HTTP)


class ProviderCallCancelled(Exception):
    """Internal unwind when the owning replay session was cancelled;
    never serialized into captures or surfaced with provider detail."""

    def __init__(self):
        super().__init__("provider call cancelled: replay_case_failed")


class CaptureCache:
    def __init__(self, root, budget, automatic_retries=False,
                 reuse_captures=True):
        self.root, self.budget, self.new_calls = root, budget, 0
        self._automatic_retries = bool(automatic_retries)
        self._reuse_captures = bool(reuse_captures)
        self._stop_retries = threading.Event()
        self._lock = threading.RLock()
        self._key_locks = {}
        self._retry_failed = contextvars.ContextVar(
            f"retry_failed_{id(self)}", default=False)
        self._call_context = contextvars.ContextVar(
            f"call_context_{id(self)}", default=None)

    @contextlib.contextmanager
    def session_scope(self, session_id, replay_id=None,
                      cancel_event=None):
        """Bind a (session, replay, cancellation) context to the
        request chains this thread runs inside the scope. Scoped via
        ContextVar: unrelated threads and calls after the scope exits
        see no context. Only session_id/replay_id are whitelisted into
        capture metadata — never labels, user text, or tokens."""
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("session scope requires a session id")
        if replay_id is not None and not isinstance(replay_id, str):
            raise ValueError("session scope replay_id must be a string")
        if cancel_event is not None and not isinstance(
                cancel_event, threading.Event):
            raise ValueError("cancel_event must be a threading.Event")
        token = self._call_context.set(
            {"session_id": session_id, "replay_id": replay_id,
             "cancel_event": cancel_event})
        try:
            yield
        finally:
            self._call_context.reset(token)

    def _scoped_cancel_event(self):
        context = self._call_context.get()
        return (context or {}).get("cancel_event")

    def _scope_cancelled(self):
        event = self._scoped_cancel_event()
        return event is not None and event.is_set()

    @staticmethod
    def _whitelisted_context(context):
        """Only session_id/replay_id may leave a scope; the cancel
        event and any other values never enter persisted metadata."""
        return {key: context[key]
                for key in ("session_id", "replay_id")
                if context.get(key) is not None}

    @staticmethod
    def _log_suffix(context, chain_attempt, cancelled_late):
        suffix = ""
        if context is not None:
            suffix = f" [session={context['session_id']}"
            if context.get("replay_id"):
                suffix += f" replay={context['replay_id']}"
            suffix += f" attempt={chain_attempt}]"
        if cancelled_late:
            suffix += " (session cancelled; response ignored)"
        return suffix

    def _cancelled_result(self, last, attempts):
        execution = copy.deepcopy(last) if last is not None else {}
        context = self._call_context.get()
        if context is not None:
            execution["call_context"] = self._whitelisted_context(
                context)
        execution.update({
            "status": "cancelled",
            "cancellation_reason": "replay_case_failed",
            "automatic_retry": {
                "attempts": attempts,
                "max_attempts": AUTOMATIC_RETRY_MAX,
                "stop_reason": "session_cancelled"}})
        return None, execution

    def stop_retries(self):
        """Cancel automatic retry chains; an in-flight network call
        cannot be cancelled, only the wait before the next attempt."""
        self._stop_retries.set()

    def _key_lock(self, base):
        with self._lock:
            lock = self._key_locks.get(base)
            if lock is None:
                lock = threading.RLock()
                self._key_locks[base] = lock
            return lock

    @contextlib.contextmanager
    def retry_failed_scope(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("retry scope requires a boolean")
        token = self._retry_failed.set(enabled)
        try:
            yield
        finally:
            self._retry_failed.reset(token)

    def path(self, provider, request):
        return self.root / f"{provider}_{digest(request)}.json"

    def _attempt_path(self, base, number):
        return base.with_name(f"{base.stem}_attempt_{number:09d}.json")

    def _attempts(self, base):
        prefix = f"{base.stem}_attempt_"
        found = []
        for candidate in base.parent.glob(f"{prefix}*.json"):
            suffix = candidate.stem[len(prefix):]
            if suffix.isdecimal() and int(suffix) >= 2:
                found.append((int(suffix), candidate))
        return sorted(found)

    def _latest_record(self, provider, request):
        base = self.path(provider, request)
        latest, number = base, 1
        for suffix, candidate in self._attempts(base):
            if suffix > number:
                latest, number = candidate, suffix
        if os.path.exists(_longpath(latest)) and not os.path.isfile(
                _longpath(latest)):
            raise ValueError("capture path is not a file")
        if not os.path.isfile(_longpath(latest)):
            return latest, None, number
        with open(_longpath(latest), encoding="utf-8") as stream:
            record = json.load(stream)
        if record["provider"] != provider or record["request"] != request:
            raise ValueError("cache request does not match its capture")
        return latest, record, number

    def _retry_delay(self, attempts, execution):
        """Backoff plus jitter, honoured Retry-After, or None when the
        server asked for more than the hard 60-second limit."""
        backoff = ((2.0 if attempts == 1 else 4.0)
                   + random.uniform(0, 0.5))
        requested = (execution.get("metadata") or {}).get(
            "retry_after_seconds")
        if requested is None:
            return backoff
        if requested > _RETRY_AFTER_LIMIT_SECONDS:
            return None
        return max(backoff, float(requested))

    def _obtain_once(self, provider, base, request, callback, adapter,
                     chain_attempt=1):
        """The original single-attempt capture/reuse semantics."""
        context = self._call_context.get()
        event = (context or {}).get("cancel_event")
        if event is not None and event.is_set():
            raise ProviderCallCancelled()
        cancelled_late = False
        with self._lock:
            if self._reuse_captures:
                path, record, number = self._latest_record(
                    provider, request)
            else:
                record, number = None, 1
                path = base.with_name(
                    f"{base.stem}_call_{secrets.token_hex(16)}.json")
            previous = path.name
            reused = record is not None and (
                record["ok"] or not self._retry_failed.get())
            if not reused:
                # Recheck before charging a new call: a request queued
                # behind the same-key lock must not execute after its
                # session has failed.
                if event is not None and event.is_set():
                    raise ProviderCallCancelled()
                retry = record is not None
                if retry:
                    path = self._attempt_path(base, number + 1)
                if self.budget is not None and self.new_calls >= self.budget:
                    raise SystemExit("new-call budget exhausted; captures retained for resume")
                self.new_calls += 1
                call_number = self.new_calls
        if not reused:
            # Once reserved, this attempt is in flight; cancellation
            # discards its result and prevents any retry.
            start = time.perf_counter()
            try:
                reply = callback()
                ok = True
            except Exception:
                reply, ok = None, False
            latency_ms = round((time.perf_counter() - start) * 1000, 3)
            metadata = adapter.last_metadata
            record = {"provider": provider, "request": request, "ok": ok,
                      "reply": reply, "metadata": metadata,
                      "original_call_latency_ms": latency_ms,
                      "origin": "hosted_replay",
                      "call_number": call_number,
                      "chain_attempt": chain_attempt}
            if context is not None:
                record["call_context"] = self._whitelisted_context(
                    context)
            if not ok:
                rejected_draft = getattr(
                    adapter, "last_rejected_draft", None)
                if rejected_draft is not None:
                    record["rejected_draft"] = rejected_draft
            if retry:
                record.update({
                    "attempt_number": number + 1,
                    "retry_of_failed_capture": True,
                    "previous_capture_file": previous})
            # A request already sent may finish; the capture is always
            # retained for auditing, but a late answer to a cancelled
            # session is never returned as usable.
            cancelled_late = event is not None and event.is_set()
            suffix = self._log_suffix(context, chain_attempt,
                                      cancelled_late)
            with self._lock:
                write_new(path, record)
                print(f"Captured {provider} call {call_number}: "
                      f"{'completed' if ok else 'failed'}{suffix}",
                      flush=True)
        execution = {"capture_file": path.name, "reused": reused,
                     "origin": record["origin"], "metadata": record["metadata"],
                     "original_call_latency_ms": record["original_call_latency_ms"]}
        for key in ("attempt_number", "retry_of_failed_capture",
                    "previous_capture_file", "call_number",
                    "chain_attempt", "call_context"):
            if key in record:
                execution[key] = record[key]
        if not record["ok"]:
            if record.get("rejected_draft") is not None:
                execution["rejected_draft"] = copy.deepcopy(
                    record["rejected_draft"])
            return None, execution
        if cancelled_late:
            return None, execution
        return copy.deepcopy(record["reply"]), execution

    def _next_retry_stop(self, execution, attempts):
        """Decide the next wait, or the reason the chain ends."""
        event = self._scoped_cancel_event()
        if event is not None and event.is_set():
            return "session_cancelled"
        if self._stop_retries.is_set():
            return "shutdown"
        if not _automatic_retryable(execution):
            return "non_retryable"
        if attempts >= AUTOMATIC_RETRY_MAX:
            return "attempts_exhausted"
        delay = self._retry_delay(attempts, execution)
        if delay is None:
            return "retry_after_too_long"
        if event is None:
            return ("shutdown"
                    if self._stop_retries.wait(delay) else None)
        # Scoped sessions wake as soon as either the session cancel or
        # the global shutdown event fires.
        deadline = time.monotonic() + delay
        while True:
            if self._stop_retries.is_set():
                return "shutdown"
            if event.is_set():
                return "session_cancelled"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            event.wait(min(0.05, remaining))

    def obtain(self, provider, request, callback, adapter):
        base = self.path(provider, request)
        with self._key_lock(base):
            try:
                reply, execution = self._obtain_once(
                    provider, base, request, callback, adapter)
            except ProviderCallCancelled:
                return self._cancelled_result(None, 0)
            attempts = 1
            if not (self._automatic_retries
                    and provider in AUTOMATIC_RETRY_PROVIDERS
                    and not execution["reused"]):
                if reply is None and self._scope_cancelled():
                    return self._cancelled_result(execution, attempts)
                return reply, execution
            stop = "success" if reply is not None else None
            while stop is None:
                stop = self._next_retry_stop(execution, attempts)
                if stop is not None:
                    break
                try:
                    with self.retry_failed_scope(True):
                        reply, execution = self._obtain_once(
                            provider, base, request, callback, adapter,
                            chain_attempt=attempts + 1)
                except ProviderCallCancelled:
                    return self._cancelled_result(execution, attempts)
                except SystemExit:
                    stop = "budget_exhausted"
                    break
                attempts += 1
                if reply is not None:
                    stop = "success"
            execution["automatic_retry"] = {
                "attempts": attempts,
                "max_attempts": AUTOMATIC_RETRY_MAX,
                "stop_reason": stop}
            if stop == "session_cancelled":
                execution["status"] = "cancelled"
                execution["cancellation_reason"] = "replay_case_failed"
            if self._scope_cancelled():
                # Cancellation observed after the final attempt (even a
                # successful one) never yields a usable reply.
                return self._cancelled_result(execution, attempts)
            return reply, execution


class CachedSelector:
    thread_safe = True

    def __init__(self, adapter, cache):
        self.adapter, self.cache = adapter, cache
        self._exec_local = threading.local()
        self._last_execution = None

    @property
    def execution(self):
        if not hasattr(self, "_exec_local"):
            self._exec_local = threading.local()
        return self._exec_local.__dict__.get(
            "value", getattr(self, "_last_execution", None))

    @execution.setter
    def execution(self, value):
        if not hasattr(self, "_exec_local"):
            self._exec_local = threading.local()
        self._exec_local.value = value
        self._last_execution = value

    def select(self, payload):
        request = selection_request(payload)
        candidates = request["state"]["candidates"]
        if len(candidates) == 1:
            self.execution = {"status": "local_single_candidate", "reused": False}
            return {"candidate_id": candidates[0]["candidate_id"]}
        reply, self.execution = self.cache.obtain(
            "jev", request, lambda: self.adapter.select(payload), self.adapter)
        if reply is None:
            raise RuntimeError("captured_selector_failure")
        return reply


class CachedGenerator:
    thread_safe = True

    def __init__(self, adapter, cache):
        self.adapter, self.cache = adapter, cache
        self._exec_local = threading.local()
        self._last_execution = None

    @property
    def execution(self):
        if not hasattr(self, "_exec_local"):
            self._exec_local = threading.local()
        return self._exec_local.__dict__.get(
            "value", getattr(self, "_last_execution", None))

    @execution.setter
    def execution(self, value):
        if not hasattr(self, "_exec_local"):
            self._exec_local = threading.local()
        self._exec_local.value = value
        self._last_execution = value

    def generate(self, payload):
        request = generation_request(payload, self.adapter._model)
        reply, self.execution = self.cache.obtain(
            "aitta", request, lambda: self.adapter.generate(payload), self.adapter)
        if reply is None:
            raise RuntimeError("captured_generator_failure")
        return reply


def seed_demo(cache, path, generator):
    """Reuse v2 smoke responses only when their full effective request matches."""
    demo = json.loads(path.read_text(encoding="utf-8"))
    if demo["mode"] != "jev_and_aitta":
        raise ValueError("seed must be a combined hosted capture")
    for package in demo["packages"]:
        review = package["review"]
        if (review["trace"]["prompt_version"] != PROMPT_VERSION
                or review["trace"]["fallback_reason"] is not None):
            raise ValueError("seed prompt version or validation does not match")
        selected = next(c for c in review["candidates"]
                        if c["candidate_id"] == review["selected_candidate_id"])
        common = {"audience": review["audience"], "checkpoint": review["checkpoint"],
                  "evidence": review["sanitized_evidence"]}
        payload = dict(common, schema=GENERATION_SCHEMA,
                       selected_candidate=selected, prompt_version=PROMPT_VERSION,
                       instructions=GENERATION_INSTRUCTIONS)
        metadata = package["generator_metadata"]
        if metadata["model_version"] != generator._model:
            raise ValueError("seed model does not match configured Aitta model")
        request = generation_request(payload, generator._model)
        target = cache.path("aitta", request)
        if not target.exists():
            write_new(target, {"provider": "aitta", "request": request, "ok": True,
                              "reply": {"candidate_id": selected["candidate_id"],
                                        "opening": review["message"]["opening"]},
                              "metadata": metadata, "original_call_latency_ms": None,
                              "origin": path.name})


def evidence_for(case, audience, checkpoint):
    return {"schema": INPUT_SCHEMA, "data_origin": "synthetic",
            "audience": audience, "checkpoint": checkpoint,
            "skills": copy.deepcopy(case["skills"][checkpoint])}


def prepare(source, output):
    if not output.exists():
        output.mkdir()
    snapshot_path = output / "inputs.json"
    if snapshot_path.exists():
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        if snapshot["schema"] != "phase3_frozen_provider_inputs_v1":
            raise ValueError("unexpected snapshot schema")
    else:
        snapshot = freeze(source)
        write_new(snapshot_path, snapshot)
    contract = {"snapshot_sha256": digest(snapshot),
                "candidate_version": CANDIDATE_VERSION, "policy_version": POLICY_VERSION,
                "selection_prompt_version": SELECTION_PROMPT_VERSION,
                "generation_prompt_version": PROMPT_VERSION,
                "generation_instructions_sha256": digest(GENERATION_INSTRUCTIONS)}
    contract_path = output / "contract.json"
    if contract_path.exists():
        if json.loads(contract_path.read_text(encoding="utf-8")) != contract:
            raise ValueError("contract changed; use a new output directory")
    else:
        write_new(contract_path, contract)
    baseline_path = output / "baselines.json"
    if not baseline_path.exists():
        write_new(baseline_path, [
            {"scenario": case["name"], "audience": audience, "checkpoint": checkpoint,
             "review": run_synthetic_feedback(evidence_for(case, audience, checkpoint))}
            for case in snapshot["cases"] for audience, checkpoint in ROLES])
    vectors = {tuple(s["correct"] for s in c["skills"]["end"])
               for c in snapshot["cases"]}
    print(f"Frozen {len(snapshot['cases'])} scenario labels; "
          f"{len(snapshot['cases']) * len(ROLES)} packages; "
          f"{len(vectors)} distinct end count vectors.", flush=True)
    print(f"At most {2 * len(vectors)} distinct Jev requests and "
          "5 distinct Aitta contexts, before reusing smoke captures.", flush=True)
    return snapshot, contract


def replay(snapshot, contract, output, selector, generator):
    package_dir = output / "packages"
    if not package_dir.exists():
        package_dir.mkdir()
    packages = []
    for case_index, case in enumerate(snapshot["cases"]):
        for role_index, (audience, checkpoint) in enumerate(ROLES):
            package_path = package_dir / f"{case_index:03d}_{role_index}.json"
            if package_path.exists():
                package = json.loads(package_path.read_text(encoding="utf-8"))
                if (package["contract"] != contract
                        or (package["scenario"], package["audience"], package["checkpoint"]) !=
                        (case["name"], audience, checkpoint)):
                    raise ValueError("saved package identity or contract changed")
            else:
                selector.execution = None
                generator.execution = None
                review = run_synthetic_feedback(
                    evidence_for(case, audience, checkpoint), selector, generator)
                baseline = review["template_baseline"]
                if (review["selected_candidate_id"] == baseline["candidate_id"]
                        and review["message"]["evidence_lines"] != baseline["evidence_lines"]):
                    raise ValueError("rendered counts changed under the same strategy")
                if checkpoint == "midpoint" and (
                        review["sanitized_evidence"] or review["message"]["evidence_lines"]):
                    raise ValueError("midpoint evidence boundary was violated")
                package = {"scenario": case["name"], "group": case["group"],
                           "audience": audience, "checkpoint": checkpoint,
                           "contract": contract, "review": review,
                           "selector_execution": copy.deepcopy(selector.execution),
                           "generator_execution": copy.deepcopy(generator.execution)}
                write_new(package_path, package)
            packages.append(package)
        print(f"Saved scenario {case_index + 1}/{len(snapshot['cases'])}: "
              f"{case['name']}", flush=True)
    end = [p for p in packages if p["checkpoint"] == "end"]
    summary = {
        "scenario_labels": len(snapshot["cases"]), "feedback_packages": len(packages),
        "end_packages": len(end),
        "end_selection_matches_rules": sum(
            p["review"]["selected_candidate_id"] ==
            p["review"]["template_baseline"]["candidate_id"] for p in end),
        "fallback_packages": sum(
            p["review"]["trace"]["fallback_reason"] is not None for p in packages),
        "new_provider_calls_this_invocation": selector.cache.new_calls,
        "independent_provider_rerun_stability_tested": False,
        "educator_preference_labels_available": False,
        "educational_effectiveness_tested": False,
    }
    report = {"schema": "phase3_provider_replay_review_v1",
              "scope": "private_synthetic_review_not_learner_delivery",
              "source": {"file": snapshot["source_file"],
                         "sha256": snapshot["source_sha256"]},
              "contract": contract,
              "reuse_policy": "identical_effective_requests_share_captured_responses_including_failures",
              "latency_warning": "cached pipeline latency is replay overhead, not hosted latency",
              "summary": summary, "packages": packages}
    write_new(output / "report.json", report)
    print(json.dumps(summary, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(
        "artifacts/assessment_pipeline_20261001.json"))
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=("prepare", "hosted"), default="prepare")
    parser.add_argument("--jev-env-file", type=Path)
    parser.add_argument("--seed-demo", type=Path)
    parser.add_argument("--max-new-calls", type=int, default=0)
    args = parser.parse_args()
    if not args.out_dir.parent.is_dir():
        parser.error("output parent must already exist")
    if args.mode == "hosted" and (
            args.jev_env_file is None or args.max_new_calls < 1):
        parser.error("hosted mode requires an external Jev key file and an explicit call budget")
    if (args.out_dir / "report.json").exists():
        parser.error("report already exists; no provider requests will be made")
    snapshot, contract = prepare(args.source, args.out_dir)
    if args.mode == "prepare":
        return
    # Force the explicit replacement-key file rather than a stale exported key.
    os.environ.pop("TYPESAFE_API_KEY", None)
    try:
        native_selector = JevSelector.from_env(args.jev_env_file)
        native_generator = AittaGenerator.from_env()
    except Exception:
        parser.error("provider configuration is invalid; secret values are not displayed")
    cache_dir = args.out_dir / "provider_captures"
    if not cache_dir.exists():
        cache_dir.mkdir()
    cache = CaptureCache(cache_dir, args.max_new_calls)
    if args.seed_demo:
        seed_demo(cache, args.seed_demo, native_generator)
    replay(snapshot, contract, args.out_dir,
           CachedSelector(native_selector, cache),
           CachedGenerator(native_generator, cache))


if __name__ == "__main__":
    main()
