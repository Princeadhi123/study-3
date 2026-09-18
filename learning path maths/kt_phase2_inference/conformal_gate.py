"""L3b -- the live gate: turn frozen-model probabilities into an intervention decision.

This is the live half of Layer 3 (conformal_calibrate.py is the offline half). It is a pure decision function over numbers the frozen
model already produced, plus the constants in
`artifacts/conformal_calibration.json`.

Explicitly NOT in scope, by design:

- no active learning, no online/continual updating, no retraining,
- no gradient ever touches the Phase-1 weights,
- no writing back to `kt_phase1/`.

The model is frozen. This module only decides how much of its output to
trust, and whether that warrants waking the Socratic LLM.


## The decision

**Item level.** The calibrated conformal prediction set is one of
`{correct}`, `{incorrect}`, `{both}`, `{}`; those map onto the three
statuses (see `conformal_calibrate.status_of`). The ambiguous band is a
consequence of the calibration, not a hand-tuned deadzone -- shrink `alpha`
and it widens, which is the intervention-budget knob.

**Checkpoint level.** Over the k items of a checkpoint the gate produces a
conformal interval on the student's success RATE, then compares it to a
mastery threshold `theta`:

    upper  <  theta  -> CONFIDENT_STRUGGLE   (mastery ruled out at 1-alpha)
    lower >=  theta  -> MASTERY_SAFE         (mastery established at 1-alpha)
    otherwise        -> UNCERTAIN_BEHAVIOR   (interval straddles theta)

`UNCERTAIN_BEHAVIOR` is the honest third state, not a failure: it says the
evidence is insufficient to act, which for a tutoring system means "keep
observing" rather than "intervene" or "advance". Conflating it with either
of the other two is what the conformal layer exists to prevent.

Only `CONFIDENT_STRUGGLE` triggers the Socratic LLM by default: that is the
state where the system can say, with a calibrated guarantee, that the
student is below mastery and remediation is warranted.


## Coverage is a property of the stream, not of one decision

A 90% guarantee means ~90% of decisions over the long run are covered. It
says nothing about whether *this* one is right. `CheckpointTriggerResult`
therefore carries `alpha`, the empirically measured coverage for its
regime, and the exchangeability caveat, so no downstream consumer can
quote the bound without also carrying its conditions.
"""
import json
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

import paths


class InterventionStatus(str, Enum):
    """What the orchestrator is entitled to conclude at 1 - alpha confidence."""

    MASTERY_SAFE = "MASTERY_SAFE"
    CONFIDENT_STRUGGLE = "CONFIDENT_STRUGGLE"
    UNCERTAIN_BEHAVIOR = "UNCERTAIN_BEHAVIOR"


@dataclass
class ItemDecision:
    """Per-item conformal prediction set and the status it implies."""

    p_correct: float
    regime: str                      # "warm" | "cold"
    prediction_set: tuple            # subset of (0, 1)
    status: InterventionStatus

    @property
    def is_ambiguous(self) -> bool:
        return len(self.prediction_set) != 1


@dataclass
class CheckpointTriggerResult:
    """The Layer-3 output consumed by the Socratic LLM orchestrator."""

    skill_id: str
    skill_name: str
    n_items: int
    regime: str
    point_estimate: float            # mean predicted success rate over the checkpoint
    lower: float                     # conformal lower bound on the realized rate
    upper: float                     # conformal upper bound
    mastery_threshold: float
    status: InterventionStatus
    alpha: float
    # Provenance / honesty fields -- a bound quoted without these is a bound
    # quoted without its conditions.
    nominal_coverage: float
    empirical_coverage: Optional[float] = None
    caveats: list = field(default_factory=list)
    item_decisions: list = field(default_factory=list)
    remediation_target: Optional[dict] = None

    @property
    def triggers_socratic_llm(self) -> bool:
        return self.status is InterventionStatus.CONFIDENT_STRUGGLE

    @property
    def interval_width(self) -> float:
        return self.upper - self.lower

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        d["triggers_socratic_llm"] = self.triggers_socratic_llm
        d["interval_width"] = self.interval_width
        d["item_decisions"] = [
            {**asdict(i), "status": i.status.value} for i in self.item_decisions
        ]
        return d


class ConformalGate:
    """Applies the offline calibration. Holds no model and no mutable state."""

    def __init__(self, calibration: dict, coverage: Optional[dict] = None,
                 skill_names: Optional[dict] = None):
        self.cal = calibration
        self.coverage = coverage or {}
        self.skill_names = skill_names or {}
        self.alpha = calibration["alpha"]
        self._item = calibration["item_level"]
        self._ckpt = calibration["checkpoint_level"]
        self.k = self._ckpt["k"]
        self.sigma_floor = self._ckpt["sigma_floor"]

    # ---- construction --------------------------------------------------
    @classmethod
    def load(cls, calibration_path: Optional[Path] = None,
             coverage_path: Optional[Path] = None,
             skill_catalog_path: Optional[Path] = None) -> "ConformalGate":
        cal_path = Path(calibration_path or paths.CALIBRATION)
        paths.require(cal_path, "Run conformal_calibrate.py first.")
        cal = json.loads(cal_path.read_text(encoding="utf-8"))

        cov_path = Path(coverage_path or paths.COVERAGE_REPORT)
        cov = json.loads(cov_path.read_text(encoding="utf-8")) if cov_path.exists() else {}

        names = {}
        cat_path = Path(skill_catalog_path or paths.SKILL_CATALOG)
        if cat_path.exists():
            import csv
            with open(cat_path, encoding="utf-8", newline="") as f:
                for row in csv.DictReader(f):
                    names[row["skill_id"]] = row["skill_name"]
        return cls(cal, cov, names)

    # ---- item level ----------------------------------------------------
    def _q(self, regime: str, label: int) -> float:
        groups = self._item["groups"]
        if regime not in groups:
            raise ValueError(f"Unknown regime {regime!r}; expected one of {sorted(groups)}")
        return groups[regime]["q_hat"][str(label)]

    def item_decision(self, p_correct: float, regime: str = "warm") -> ItemDecision:
        """Conformal prediction set for one upcoming item.

        `regime` is "cold" when the item is not in the model's `item_vocab`
        (it resolves to `__UNK__`), "warm" otherwise -- known at inference
        time, which is what makes it a valid Mondrian taxonomy.
        """
        s = []
        if p_correct <= self._q(regime, 0):       # 1 - p(incorrect) <= q0
            s.append(0)
        if (1.0 - p_correct) <= self._q(regime, 1):
            s.append(1)
        s = tuple(s)
        if s == (1,):
            status = InterventionStatus.MASTERY_SAFE
        elif s == (0,):
            status = InterventionStatus.CONFIDENT_STRUGGLE
        else:
            # (0, 1) = cannot rule either out; () = both labels non-conforming,
            # i.e. atypical of everything seen in calibration. Both are
            # abstentions, not confident calls.
            status = InterventionStatus.UNCERTAIN_BEHAVIOR
        return ItemDecision(float(p_correct), regime, s, status)

    # ---- checkpoint level ----------------------------------------------
    def _empirical_coverage(self, regime: str) -> Optional[float]:
        for block in self.coverage.get("checkpoint_level", []):
            if block.get("label") == f"eval_{regime}":
                return block.get("empirical_coverage")
        return None

    def checkpoint(self, probs: Sequence[float], skill_id: str,
                   regime: str = "warm", mastery_threshold: float = 0.80,
                   remediation_lookup: Optional[Callable[[str], Optional[dict]]] = None,
                   ) -> CheckpointTriggerResult:
        """Conformal interval on the success RATE over a checkpoint, plus the gate decision.

        `probs` are the frozen model's P(correct) for the k items of the
        checkpoint. The interval is `mean(p) +- q_hat * sigma`, where sigma
        is the Poisson-binomial SD the model itself implies for these
        specific items -- so the width reflects this student's actual
        predicted uncertainty instead of being the same constant for
        everyone.
        """
        p = np.asarray(probs, dtype=np.float64)
        if p.size == 0:
            raise ValueError("A checkpoint needs at least one item.")
        caveats = list(self.cal.get("calibration_protocol", {}).get(
            "exchangeability_caveat", "") and
            [self.cal["calibration_protocol"]["exchangeability_caveat"]] or [])
        if p.size != self.k:
            # Not fatal -- sigma adapts to the actual count -- but the
            # quantile was calibrated at k, so say so rather than pretend.
            caveats.append(
                f"Checkpoint has {p.size} items but q_hat was calibrated at k={self.k}; "
                f"coverage is approximate away from the calibrated block size."
            )

        group = self._ckpt["groups"].get(regime) or self._ckpt["pooled"]
        q = group["q_hat"]
        mean_p = float(p.mean())
        sigma = max(float(np.sqrt((p * (1 - p)).sum()) / p.size), self.sigma_floor)
        lower = float(np.clip(mean_p - q * sigma, 0.0, 1.0))
        upper = float(np.clip(mean_p + q * sigma, 0.0, 1.0))

        if upper < mastery_threshold:
            status = InterventionStatus.CONFIDENT_STRUGGLE
        elif lower >= mastery_threshold:
            status = InterventionStatus.MASTERY_SAFE
        else:
            status = InterventionStatus.UNCERTAIN_BEHAVIOR

        remediation = None
        if status is InterventionStatus.CONFIDENT_STRUGGLE and remediation_lookup is not None:
            # The knowledge graph answers "what should we drop back to?".
            # Resolved here rather than by the caller so the trigger result
            # is self-contained for the Socratic LLM.
            remediation = remediation_lookup(skill_id)

        return CheckpointTriggerResult(
            skill_id=skill_id,
            skill_name=self.skill_names.get(skill_id, skill_id),
            n_items=int(p.size),
            regime=regime,
            point_estimate=mean_p,
            lower=lower,
            upper=upper,
            mastery_threshold=mastery_threshold,
            status=status,
            alpha=self.alpha,
            nominal_coverage=1.0 - self.alpha,
            empirical_coverage=self._empirical_coverage(regime),
            caveats=caveats,
            item_decisions=[self.item_decision(float(x), regime) for x in p],
            remediation_target=remediation,
        )


if __name__ == "__main__":
    # Smoke demo against whatever calibration currently exists.
    gate = ConformalGate.load()
    demo = gate.checkpoint([0.42, 0.51, 0.33, 0.6, 0.47, 0.38, 0.55, 0.41, 0.49, 0.36],
                           skill_id="skill_example", regime="warm")
    print(json.dumps(demo.to_dict(), indent=2, ensure_ascii=False))
