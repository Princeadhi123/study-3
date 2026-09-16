"""Layer 3, step 1: uncertainty-aware state extraction via split conformal
prediction (SCP).

The frozen KT model (see ../modeling/model.py) only ever emits a point
estimate P(correct) per (student, skill) -- it has no notion of its own
confidence. Split conformal prediction is used here purely as a *post-hoc*
statistical wrapper around that point estimate: it does not touch or
retrain the model, it only uses a held-out calibration set of (point
estimate, realized outcome) pairs to turn each point estimate into a
finite-sample-valid interval, at a user-chosen miscoverage rate `alpha`.

This gives the orchestrator a *guarantee* ("with probability >= 1 - alpha,
the student's true correctness probability on this skill lies in
[lower, upper]") rather than trusting the bare point estimate, which is
what lets us safely say a skill is "confidently weak" instead of just
"the model's point estimate happened to be low".
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class CalibrationSample:
    """One held-out (never used for KT training) (prediction, outcome) pair.

    `point_estimate` is the frozen KT model's P(correct) for some
    (student, skill, timestep) triple; `true_label` is what the student
    actually did next (1 = correct, 0 = incorrect). This is exactly the
    kind of data already produced by the KT eval/inference pass -- no new
    model output is needed to build a calibration set, only a held-out
    slice of interactions that were not used to fit the KT model itself.
    """

    point_estimate: float
    true_label: int


@dataclass(frozen=True)
class SkillMasteryInterval:
    """A conformal interval for one skill's next-interaction correctness
    probability, plus the point estimate it was built around."""

    skill_id: str
    point_estimate: float
    lower: float
    upper: float

    @property
    def interval_width(self) -> float:
        return self.upper - self.lower


def compute_conformal_quantile(
    calibration_samples: Sequence[CalibrationSample], alpha: float = 0.10
) -> float:
    """Standard split-conformal quantile of the absolute-residual
    nonconformity score `|y_true - p_hat|` over the calibration set.

    Uses the finite-sample correction ceil((n + 1) * (1 - alpha)) / n
    (Vovk et al.) rather than a plain (1 - alpha) quantile, which is what
    gives split conformal prediction its distribution-free coverage
    guarantee (marginal coverage >= 1 - alpha) for any n, not just
    asymptotically.
    """
    if not calibration_samples:
        raise ValueError("calibration_samples must be non-empty")
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")

    scores = np.array(
        [abs(s.true_label - s.point_estimate) for s in calibration_samples],
        dtype=np.float64,
    )
    n = len(scores)
    rank = int(np.ceil((n + 1) * (1 - alpha)))
    rank = min(rank, n)  # cap: with alpha this small / n this small, ceil() can exceed n
    scores_sorted = np.sort(scores)
    return float(scores_sorted[rank - 1])


def build_prediction_interval(point_estimate: float, q_hat: float) -> tuple[float, float]:
    """[p_hat - q_hat, p_hat + q_hat], clipped to the valid probability
    range [0, 1] (clipping does not affect the coverage guarantee, it only
    ever tightens an interval that had walked outside a range the true
    probability could never occupy anyway)."""
    lower = max(0.0, point_estimate - q_hat)
    upper = min(1.0, point_estimate + q_hat)
    return lower, upper


def extract_low_mastery_skill(
    mastery_state: dict[str, float],
    calibration_samples: Sequence[CalibrationSample],
    alpha: float = 0.10,
    mastery_threshold: float = 0.5,
) -> SkillMasteryInterval | None:
    """Given a student's current per-skill point-estimate mastery vector
    (`mastery_state`, e.g. {"fractions.add_unlike_denom": 0.42, ...} straight
    off the frozen KT model, already mapped onto the prerequisite graph), find
    the skill the orchestrator should intervene on next.

    A skill only qualifies if the conformal interval's UPPER bound is below
    `mastery_threshold` -- i.e. even in the most optimistic case consistent
    with the calibration guarantee, the model does not believe the student
    has mastered it. This is deliberately stricter than thresholding the
    point estimate alone: it will not flag a skill just because a noisy
    point estimate dipped low, only one where the interval as a whole sits
    below the bar.

    Among qualifying skills, returns the one with the lowest upper bound
    (the skill the model is most confident is weak) as the intervention
    target, breaking ties by narrower interval (i.e. prefer the more
    precisely-pinned-down estimate). Returns None if no skill qualifies.
    """
    if not mastery_state:
        raise ValueError("mastery_state must be non-empty")

    q_hat = compute_conformal_quantile(calibration_samples, alpha=alpha)

    candidates: list[SkillMasteryInterval] = []
    for skill_id, p_hat in mastery_state.items():
        lower, upper = build_prediction_interval(p_hat, q_hat)
        candidates.append(SkillMasteryInterval(skill_id, p_hat, lower, upper))

    qualifying = [c for c in candidates if c.upper < mastery_threshold]
    if not qualifying:
        return None

    qualifying.sort(key=lambda c: (c.upper, c.interval_width))
    return qualifying[0]
