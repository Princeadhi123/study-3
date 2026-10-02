"""Fresh session diagnostics from frozen weights and historical calibration.

These are private research results, never authority for student feedback.
The assessment graph uses observed answers. KT/conformal use current-session
forward passes, not saved scenario predictions, with no training or updates.
"""
import hashlib
import importlib
import math
import threading
from pathlib import Path

import phase3_paths
from feedback_service import assessment_feedback_graph
from kt_adapter import trace_responses
from session_store import utc_now

SCOPE_WARNING = (
    "Research diagnostics only. The frozen KT model and historical conformal "
    "calibration are not validated mastery measures for this fixed assessment. "
    "Raw gate labels do not establish mastery or diagnose a misconception, "
    "learning, decline, or fatigue. They do not control student advice."
)


def _fingerprint(path):
    path = Path(path)
    return {"file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class LiveDiagnostics:
    """Lazy, serialized access to one frozen model instance per demo process."""

    def __init__(self, model_loader=None, gate_loader=None):
        self._lock = threading.RLock()
        self._model_loader = model_loader
        self._gate_loader = gate_loader
        self._kt = None
        self._gates = None
        self._provenance = {}
        self._state = "not_loaded"

    @property
    def state(self):
        # Model loading can be slow; callers must not wait on its compute lock.
        return {"status": self._state, "mode": "fresh_frozen_model_inference",
                "scope_warning": SCOPE_WARNING}

    def warmup(self):
        with self._lock:
            if self._state in ("ready", "unavailable"):
                return self.state
            self._state = "loading"
            try:
                if self._model_loader is None:
                    self._kt = importlib.import_module("frozen_model").load_frozen_model(
                        device="cpu")
                else:
                    self._kt = self._model_loader()
                if self._gate_loader is None:
                    phase2_paths = importlib.import_module("paths")
                    gate_class = importlib.import_module("conformal_gate").ConformalGate
                    self._gates = {
                        "midpoint": gate_class.load(
                            calibration_path=phase2_paths.HISTORICAL_K5_CALIBRATION,
                            coverage_path=phase2_paths.HISTORICAL_K5_COVERAGE),
                        "end": gate_class.load()}
                    self._provenance = {
                        "checkpoint": _fingerprint(phase2_paths.CHECKPOINT),
                        "configuration": _fingerprint(phase2_paths.RUN_CONFIG),
                        "vocabulary": _fingerprint(phase2_paths.VOCAB),
                        "midpoint_calibration": _fingerprint(
                            phase2_paths.HISTORICAL_K5_CALIBRATION),
                        "midpoint_coverage": _fingerprint(
                            phase2_paths.HISTORICAL_K5_COVERAGE),
                        "end_calibration": _fingerprint(phase2_paths.ACTIVE_CALIBRATION),
                        "end_coverage": _fingerprint(phase2_paths.ACTIVE_COVERAGE_REPORT),
                        "embeddings": {
                            "file": phase2_paths.TEXT_EMBEDDINGS.name,
                            "verification": (
                                "existing strict loader checks content dimension "
                                "and exact content/option coverage; no full-table hash"),
                            "bytes": phase2_paths.TEXT_EMBEDDINGS.stat().st_size}}
                else:
                    self._gates = self._gate_loader()
                if (self._gates["midpoint"].k != 5 or self._gates["end"].k != 10):
                    raise ValueError("live checkpoints require existing k5/k10 calibrations")
                self._state = "ready"
            except Exception:
                self._kt, self._gates = None, None
                self._state = "unavailable"
            return self.state

    def evaluate(self, bank, taxonomy, responses):
        graph = assessment_feedback_graph(bank, taxonomy, responses)
        checkpoint = "midpoint" if len(responses) == 20 else "end"
        base = {
            "schema": "phase3_live_research_diagnostics_v1",
            "mode": "fresh_frozen_model_inference", "checkpoint": checkpoint,
            "answer_count": len(responses), "graph": graph,
            "scope_warning": SCOPE_WARNING, "used_for_student_advice": False}
        with self._lock:
            self.warmup()
            if self._state != "ready":
                return {**base, "status": "unavailable",
                        "reason": "frozen_model_or_calibration_inputs_unavailable",
                        "kt": {"status": "unavailable"},
                        "conformal": {"status": "unavailable"}, "computed_at": utc_now()}
            trace = trace_responses(bank, responses, kt=self._kt, device="cpu")
            probabilities = trace["p_correct_before_each_answer"]
            if (len(probabilities) != len(responses)
                    or any(type(p) not in (int, float) or not math.isfinite(p)
                           or not 0 <= p <= 1 for p in probabilities)):
                raise ValueError("live KT trace must have one finite probability per answer")
            coverage = trace["coverage"]
            unknown = set(coverage["unknown_item_ids"])
            questions = bank["questions"][:len(responses)]
            regimes = ["cold" if q["item_id"] in unknown else "warm" for q in questions]
            gate = self._gates[checkpoint]
            skill_results = {}
            for sid in bank["skill_names"]:
                indexes = [i for i, q in enumerate(questions) if q["skill_id"] == sid]
                if len(indexes) != gate.k:
                    raise ValueError("live skill checkpoint size disagrees with calibration")
                regime = "cold" if any(regimes[i] == "cold" for i in indexes) else "warm"
                result = gate.checkpoint(
                    [probabilities[i] for i in indexes], sid, regime=regime).to_dict()
                result["skill_name"] = bank["skill_names"][sid]
                result["used_for_student_advice"] = False
                skill_results[sid] = result
            items = []
            for index, (probability, regime) in enumerate(zip(probabilities, regimes)):
                decision = gate.item_decision(probability, regime)
                items.append({
                    "position": index + 1, "p_correct_before_answer": probability,
                    "regime": regime, "prediction_set": list(decision.prediction_set),
                    "raw_historical_gate_label": decision.status.value})
            return {
                **base, "status": "ready", "computed_at": utc_now(),
                "kt": {
                    "status": trace["model_status"], "kind": trace["kind"],
                    "p_correct_before_each_answer": probabilities,
                    "input_policy": trace["input_policy"],
                    "coverage": {
                        "question_count": coverage["question_count"],
                        "unknown_item_count": len(coverage["unknown_item_ids"]),
                        "missing_skill_count": len(coverage["missing_skill_indexes"]),
                        "missing_content_count": len(coverage["missing_content_indexes"]),
                        "missing_option_count": len(coverage["missing_option_values"])},
                    "provenance": self._provenance},
                "conformal": {
                    "status": "exploratory_only_not_fixed_bank_validated",
                    "calibrated_k": gate.k, "skills": skill_results, "items": items,
                    "scope_warning": SCOPE_WARNING}}
