import copy
import hashlib
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import phase3_paths
from evidence_feedback import canonical_digest
from research_workspace import ID_RE, ResearchConflict
from schemas import validate_submission
from session_store import bank_fingerprint, utc_now


DEFAULT_SOURCE = Path(__file__).parent / "artifacts" / "assessment_pipeline_20261001.json"
ACTIVE = {"queued", "running", "cancelling"}


class ScenarioReplays:
    def __init__(self, service, source_path=DEFAULT_SOURCE):
        self.service = service
        self.source_path = Path(source_path)
        self.directory = service.root / "replay_runs"
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scenario-replay")
        self.code = self.current_code()
        for path in self.directory.glob("*.json"):
            record = self._read(path.stem)
            if record["status"] in ACTIVE:
                record["status"] = "interrupted"
                record["finished_at"] = utc_now()
                for case in record["cases"]:
                    if case["status"] in ACTIVE:
                        case["status"] = "interrupted"
                self._save(record)

    def current_code(self):
        names = ("demo_service.py", "scenario_replays.py", "research_workspace.py",
                 "evidence_feedback.py", "evidence_feedback_policy.py", "evidence_providers.py",
                 "feedback_service.py", "mcq_service.py", "live_diagnostics.py", "kt_adapter.py",
                 "integrated_synthetic_pipeline.py", "replay_provider_scenarios.py",
                 "jev_selector.py", "aitta_generator.py", "synthetic_feedback.py",
                 "research_runtime.py", "active_payload.py")
        root = Path(__file__).parent
        paths = {name: root / name for name in names}
        paths.update({f"phase2/{name}": phase3_paths.PHASE2_ROOT / name for name in
                      ("frozen_model.py", "conformal_gate.py", "question_bank.py", "test_feed.py", "paths.py")})
        return {name: hashlib.sha256(path.read_bytes()).hexdigest() for name, path in paths.items()}

    def _path(self, run_id):
        if not isinstance(run_id, str) or not ID_RE.fullmatch(run_id):
            raise ValueError("invalid replay id")
        return self.directory / f"{run_id}.json"

    def _read(self, run_id):
        return json.loads(self._path(run_id).read_text(encoding="utf-8"))

    def _save(self, record):
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self._path(record["id"])
        temporary = target.with_suffix(f".{secrets.token_hex(6)}.tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)

    def source_cases(self):
        library = self.service.research.library()
        if library["status"] != "ready":
            raise ValueError("replay report unavailable")
        bank_mode = self.service.replay_bank_mode
        context = self.service._bank_context(bank_mode)
        bank, taxonomy = context["bank"], context["taxonomy"]
        raw = self.source_path.read_bytes()
        source = json.loads(raw)
        if (not isinstance(source, dict)
                or library["source"].get("bank_mode", "demo") != bank_mode
                or source.get("bank_mode", "demo") != bank_mode
                or hashlib.sha256(raw).hexdigest() != library["source"].get("sha256")
                or source.get("schema") != "phase3_fixed40_comparison_v1"
                or source.get("scope") != "private_synthetic_fixed_40_cold_start_not_student_validation"
                or source.get("bank_sha256") != bank_fingerprint(bank)
                or source["bank_sha256"] != library["source"]["bank_sha256"]):
            raise ValueError("original response source is missing or incompatible")
        if bank_mode != "demo" and (
                source.get("taxonomy_sha256") != canonical_digest(taxonomy)
                or library["source"].get("taxonomy_sha256") != canonical_digest(taxonomy)
                or source.get("bank_provenance") != context["provenance"]):
            raise ValueError("original response source is missing or incompatible")
        cases = source["scenarios"]
        by_name = {case["name"]: case for case in cases}
        if len(by_name) != len(cases):
            raise ValueError("duplicate source scenario")
        result = {}
        for item in library["scenarios"]:
            case = by_name[item["name"]]
            rows = case["responses"]
            if len(rows) != 40:
                raise ValueError("expected forty original responses")
            submissions = []
            for index, (row, question) in enumerate(zip(rows, bank["questions"])):
                submission = {"question_id": row["question_id"], "selected_index": row["selected_index"]}
                validate_submission(submission, question, index)
                submissions.append(submission)
            correct = sum(row["selected_index"] == q["answer_index"] for row, q in
                          zip(submissions, bank["questions"]))
            if correct != item["total"]["correct"]:
                raise ValueError("saved report disagrees with original responses")
            result[item["id"]] = submissions
        return result

    def list(self):
        with self.lock:
            runs = [self._view(record) for record in
                    (self._read(path.stem) for path in self.directory.glob("*.json"))
                    if record.get("bank_mode", "demo") == self.service.replay_bank_mode]
        return {"runs": sorted(runs, key=lambda r: r["created_at"], reverse=True),
                "restart_required": self.current_code() != self.code,
                "configured_provider_mode": self.service.provider_mode}

    def _view(self, record):
        view = copy.deepcopy(record)
        view.pop("request", None)
        view["completed"] = sum(c["status"] == "complete" for c in record["cases"])
        view["failed"] = sum(c["status"] == "failed" for c in record["cases"])
        view["total"] = len(record["cases"])
        return view

    def get(self, run_id):
        with self.lock:
            return self._view(self._read(run_id))

    def _result_path(self, session_id):
        if not isinstance(session_id, str) or not ID_RE.fullmatch(session_id):
            raise ValueError("invalid session id")
        return self.directory / "results" / f"{session_id}.json"

    def _save_result(self, detail):
        with self.lock:
            path = self._result_path(detail["session_id"])
            path.parent.mkdir(exist_ok=True)
            with path.open("x", encoding="utf-8") as stream:
                json.dump(detail, stream, ensure_ascii=False, indent=2)

    def sync_reviews(self, session_id, reviews):
        with self.lock:
            path = self._result_path(session_id)
            if path.is_file():
                detail = json.loads(path.read_text(encoding="utf-8"))
                detail["reviews"] = copy.deepcopy(reviews)
                temporary = path.with_suffix(f".{secrets.token_hex(6)}.tmp")
                temporary.write_text(json.dumps(detail, ensure_ascii=False, indent=2), encoding="utf-8")
                temporary.replace(path)

    def result(self, run_id, case_id):
        record = self.get(run_id)
        row = next((c for c in record["cases"] if c["scenario_id"] == case_id), None)
        if row is None or row.get("session_id") is None:
            raise FileNotFoundError("replay result not yet available")
        try:
            detail = self.service.teacher_session(row["session_id"])
        except (ValueError, FileNotFoundError):
            with self.lock:
                detail = json.loads(self._result_path(row["session_id"]).read_text(encoding="utf-8"))
            detail.update({"read_only": True, "review_hashes": {}, "source_mode": "saved_replay",
                           "read_only_reason": "This replay belongs to an older bank, taxonomy, or policy version. Its result and review notes are preserved. Replay the scenario under the current version to add new reviews."})
        detail["replay_changes"] = row.get("changes")
        detail["export_url"] = f"/api/teacher/replays/{run_id}/cases/{case_id}/export"
        return detail

    def start(self, body):
        if set(body) != {"request_id", "report_sha256", "scenario_ids", "provider_mode", "allow_provider_calls"}:
            raise ValueError("invalid replay request")
        self._path(body["request_id"])
        ids = body["scenario_ids"]
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 54
                or any(not isinstance(i, str) or not ID_RE.fullmatch(i) for i in ids)
                or len(set(ids)) != len(ids) or type(body["allow_provider_calls"]) is not bool
                or body["provider_mode"] not in ("rules", "hosted")):
            raise ValueError("invalid replay selection")
        if body["provider_mode"] == "hosted" and (
                self.service.provider_mode != "hosted" or not body["allow_provider_calls"]):
            raise ValueError("hosted replay requires configuration and explicit consent")
        with self.lock:
            if self._path(body["request_id"]).exists():
                existing = self._read(body["request_id"])
                if existing["request"] != body:
                    raise ResearchConflict("request id already used")
                return self._view(existing)
            if self.stop.is_set() or self.current_code() != self.code:
                raise ResearchConflict("restart server before replaying changed code")
            if any(r["status"] in ACTIVE for r in self.list()["runs"]):
                raise ResearchConflict("another replay is active")
            library = self.service.research.library()
            if library.get("report_sha256") != body["report_sha256"]:
                raise ResearchConflict("saved report revision changed")
            index = {row["id"]: row for row in library["scenarios"]}
            if any(i not in index for i in ids):
                raise ValueError("unknown saved case")
            try:
                submissions = self.source_cases()
            except (OSError, KeyError, TypeError, ValueError):
                raise ValueError("original response source unavailable or incompatible") from None
            record = {"id": body["request_id"], "schema": "phase3_scenario_replay_run_v1",
                      "bank_mode": self.service.replay_bank_mode,
                      "request": copy.deepcopy(body), "created_at": utc_now(), "status": "queued",
                      "report_sha256": body["report_sha256"], "source": library["source"],
                      "code_sha256": self.code, "provider_mode": body["provider_mode"],
                      "diagnostics_mode": "fresh_frozen_model_inference", "cancel_requested": False,
                      "cases": [{"scenario_id": i, "name": index[i]["name"],
                                 "display_name": index[i]["display_name"], "status": "queued",
                                 "session_id": None} for i in ids]}
            self._save(record)
            self.pool.submit(self._run, record["id"], submissions)
            return self._view(record)

    def cancel(self, run_id):
        with self.lock:
            record = self._read(run_id)
            if record["status"] in ACTIVE:
                record["cancel_requested"] = True
                record["status"] = "cancelling"
                self._save(record)
            return self._view(record)

    def _changes(self, case_id, detail):
        original = self.service.research.scenario(case_id)
        packages = {p["audience"]: p["review"] for p in original["packages"] if p["checkpoint"] == "end"}
        end = detail["checkpoints"]["end"]
        comparisons = []
        for audience, previous in packages.items():
            current = end[f"{audience}_review"]
            comparisons.append({"audience": audience,
                                "candidate_changed": previous["selected_candidate_id"] != current["selected_candidate_id"],
                                "text_changed": previous["message"]["text"] != current["message"]["text"],
                                "previous_text": previous["message"]["text"], "current_text": current["message"]["text"]})
        old_probs = original.get("research_diagnostics", {}).get("kt", {}).get("p_correct_before_each_answer", [])
        new_probs = (end.get("diagnostics") or {}).get("kt", {}).get("p_correct_before_each_answer", [])
        return {"observed_correct_delta": detail["observed_total"]["correct"] - packages["student"]["sanitized_evidence"]["total"]["correct"],
                "feedback": comparisons,
                "kt_max_absolute_delta": max(abs(a - b) for a, b in zip(old_probs, new_probs))
                if len(old_probs) == len(new_probs) == 40 else None,
                "interpretation": "Changes relative to the retained original, not evidence of improvement."}

    def _run(self, run_id, submissions):
        with self.lock:
            record = self._read(run_id)
            if record["status"] == "queued":
                record["status"] = "running"
            self._save(record)
        for index in range(len(record["cases"])):
            with self.lock:
                record = self._read(run_id)
                if record["cancel_requested"] or self.stop.is_set():
                    break
                row = record["cases"][index]
                row["status"] = "running"
                self._save(record)
            try:
                replay = {"run_id": run_id, "scenario_id": row["scenario_id"],
                          "scenario_name": row["name"], "report_sha256": record["report_sha256"]}
                created = self.service.create_session(label=row["display_name"], replay=replay,
                                                      provider_mode=record["provider_mode"],
                                                      bank_mode=record.get("bank_mode", "demo"))
                session_id, token = created["session_id"], created["student_token"]
                with self.lock:
                    record = self._read(run_id)
                    record["cases"][index]["session_id"] = session_id
                    self._save(record)
                for submission in submissions[row["scenario_id"]]:
                    if self.stop.is_set():
                        break
                    self.service.submit_response(session_id, token, submission)
                while not self.stop.is_set():
                    detail = self.service.teacher_session(session_id)
                    if (detail["provider_job"]["status"] != "pending" and
                            all(c["diagnostics_job"]["status"] != "pending"
                                for c in detail["checkpoints"].values())):
                        break
                    self.stop.wait(.1)
                if self.stop.is_set():
                    break
                with self.service.lock:
                    detail = self.service.teacher_session(session_id)
                    self._save_result(detail)
                changes = self._changes(row["scenario_id"], detail)
                outcome = {"status": "complete", "changes": changes,
                           "diagnostics": {cp: block["diagnostics_job"]["status"] for cp, block in detail["checkpoints"].items()},
                           "provider_status": detail["provider_job"]["status"], "finished_at": utc_now()}
            except Exception:
                outcome = {"status": "failed", "reason": "replay_case_failed", "finished_at": utc_now()}
            with self.lock:
                record = self._read(run_id)
                record["cases"][index].update(outcome)
                self._save(record)
        with self.lock:
            record = self._read(run_id)
            record["status"] = ("interrupted" if self.stop.is_set() else "cancelled" if record["cancel_requested"]
                                else "complete_with_errors" if any(c["status"] == "failed" for c in record["cases"]) else "complete")
            for row in record["cases"]:
                if row["status"] in ACTIVE:
                    row["status"] = "interrupted" if self.stop.is_set() else "cancelled"
            record["finished_at"] = utc_now()
            self._save(record)

    def close(self, wait=False):
        self.stop.set()
        self.pool.shutdown(wait=wait)
