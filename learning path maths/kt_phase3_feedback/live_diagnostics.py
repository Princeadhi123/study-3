"""Teacher-private frozen KT traces; observed answers remain authoritative."""
import hashlib
import importlib
import math
import threading
from pathlib import Path

import phase3_paths
from feedback_service import assessment_feedback_graph
from kt_adapter import trace_responses
from research_runtime import require_augmented_embeddings
from session_store import utc_now

SCOPE_WARNING = (
    "Research diagnostics only. Frozen KT estimates do not establish mastery, "
    "difficulty, learning benefit, or causes of incorrect answers. "
    "They do not control student advice."
)


def _fingerprint(path):
    path = Path(path)
    return {"file": path.name,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


class LiveDiagnostics:
    """Lazy, serialized access to one frozen model instance per demo process."""

    def __init__(self, model_loader=None):
        self._lock = threading.RLock()
        self._model_loader = model_loader
        self._kt = None
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
                if self._kt is None:
                    if self._model_loader is None:
                        self._kt = importlib.import_module("frozen_model").load_frozen_model(
                            device="cpu")
                    else:
                        self._kt = self._model_loader()
                if self._model_loader is None:
                    phase2_paths = importlib.import_module("paths")
                    self._provenance = {
                        "checkpoint": _fingerprint(phase2_paths.CHECKPOINT),
                        "configuration": _fingerprint(phase2_paths.RUN_CONFIG),
                        "vocabulary": _fingerprint(phase2_paths.VOCAB),
                        "embeddings": {
                            "file": phase2_paths.TEXT_EMBEDDINGS.name,
                            "verification": (
                                "existing strict loader checks content dimension "
                                "and exact content/option coverage; no full-table hash"),
                            "bytes": phase2_paths.TEXT_EMBEDDINGS.stat().st_size}}
                self._state = "ready"
            except Exception:
                self._kt = None
                self._state = "unavailable"
            return self.state

    def predict_future_candidates(self, bank, responses, candidates):
        """Live practice requires the explicitly selected augmented table."""
        require_augmented_embeddings()
        with self._lock:
            if self._kt is None:
                if self._model_loader is None:
                    self._kt = importlib.import_module("frozen_model").load_frozen_model(
                        device="cpu")
                else:
                    self._kt = self._model_loader()
            return importlib.import_module(
                "future_kt").predict_future_candidates(
                    bank, responses, candidates, self._kt)

    def evaluate(self, bank, taxonomy, responses):
        graph = assessment_feedback_graph(bank, taxonomy, responses)
        checkpoint = "midpoint" if len(responses) == 20 else "end"
        base = {
            "schema": "phase3_live_research_diagnostics_v2",
            "mode": "fresh_frozen_model_inference", "checkpoint": checkpoint,
            "answer_count": len(responses), "graph": graph,
            "scope_warning": SCOPE_WARNING, "used_for_student_advice": False}
        with self._lock:
            self.warmup()
            if self._state != "ready":
                return {**base, "status": "unavailable",
                        "reason": "frozen_model_inputs_unavailable",
                        "kt": {"status": "unavailable"},
                        "computed_at": utc_now()}
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
            items = [{"position": index + 1, "regime": regime}
                     for index, regime in enumerate(regimes)]
            return {
                **base, "status": "ready", "computed_at": utc_now(),
                "kt": {
                    "status": trace["model_status"], "kind": trace["kind"],
                    "p_correct_before_each_answer": probabilities,
                    "items": items,
                    "input_policy": trace["input_policy"],
                    "coverage": {
                        "question_count": coverage["question_count"],
                        "unknown_item_count": len(coverage["unknown_item_ids"]),
                        "missing_skill_count": len(coverage["missing_skill_indexes"]),
                        "missing_content_count": len(coverage["missing_content_indexes"]),
                        "missing_option_count": len(coverage["missing_option_values"])},
                    "provenance": self._provenance}}
