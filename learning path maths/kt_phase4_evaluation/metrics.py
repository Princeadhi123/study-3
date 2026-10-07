"""Lead-defined event metrics and paired base-student cluster uncertainty."""
import numpy as np

from phase4_common import BOOTSTRAP_DRAWS, LOG_EPS, SEED


def weighted_auc(y, probability, weight):
    order = np.argsort(probability, kind="stable")
    sorted_p = probability[order]
    starts = np.r_[True, sorted_p[1:] != sorted_p[:-1]]
    groups = np.cumsum(starts) - 1
    positives = np.bincount(groups, weights=weight[order] * y[order])
    negatives = np.bincount(groups, weights=weight[order] * (1 - y[order]))
    n_positive, n_negative = positives.sum(), negatives.sum()
    if n_positive == 0 or n_negative == 0:
        return None
    prior_negative = np.cumsum(negatives) - negatives
    return float(np.sum(positives * (prior_negative + negatives / 2)) / (
        n_positive * n_negative))


def scores(y, probability, weight, cluster):
    denominator = weight.sum()
    if denominator <= 0:
        raise ValueError("Empty weighted evaluation")
    p = np.clip(probability, LOG_EPS, 1 - LOG_EPS)
    loss = -(y * np.log(p) + (1 - y) * np.log1p(-p))
    squared = (probability - y) ** 2
    cluster_n = np.bincount(cluster)
    balanced = weight / cluster_n[cluster]
    return {
        "auc": weighted_auc(y, probability, weight),
        "log_loss": float(np.dot(weight, loss) / denominator),
        "brier": float(np.dot(weight, squared) / denominator),
        "student_balanced_log_loss": float(np.dot(balanced, loss) / balanced.sum()),
        "student_balanced_brier": float(np.dot(balanced, squared) / balanced.sum()),
    }


def interval(values, n_students):
    valid = [float(v) for v in values if v is not None and np.isfinite(v)]
    if n_students < 2 or len(valid) < 100:
        return {"lower": None, "upper": None, "valid_draws": len(valid),
                "reason": "fewer_than_two_students_or_100_valid_draws"}
    lower, upper = np.quantile(valid, [0.025, 0.975])
    return {"lower": float(lower), "upper": float(upper), "valid_draws": len(valid),
            "reason": None}


def calibration_bins(y, probability):
    bins = np.minimum((probability * 10).astype(int), 9)
    result, ece = [], 0.0
    for index in range(10):
        mask = bins == index
        n = int(mask.sum())
        predicted = float(probability[mask].mean()) if n else None
        observed = float(y[mask].mean()) if n else None
        if n:
            ece += n / len(y) * abs(predicted - observed)
        result.append({"lower": index / 10, "upper": (index + 1) / 10,
                       "n": n, "mean_probability": predicted, "observed_rate": observed})
    return {"bins": result, "ece": float(ece)}


def evaluate(rows, columns, pairs=(), draws=BOOTSTRAP_DRAWS, frozen=None):
    if not rows:
        return {"status": "no_eligible_targets", "events": 0}
    y = np.asarray([r["y"] for r in rows], dtype=float)
    if not np.isin(y, [0, 1]).all():
        raise ValueError("Non-binary labels")
    students, cluster = np.unique([r["student_code"] for r in rows], return_inverse=True)
    probabilities = {name: np.asarray([r[name] for r in rows], dtype=float) for name in columns}
    for p in probabilities.values():
        if not np.isfinite(p).all() or np.any((p < 0) | (p > 1)):
            raise ValueError("Invalid probabilities")
    frozen = frozen or {}
    metric_names = ("auc", "log_loss", "brier", "student_balanced_log_loss", "student_balanced_brier")
    points = {name: {key: frozen[name][key] for key in metric_names} if name in frozen
              else scores(y, p, np.ones(len(y)), cluster)
              for name, p in probabilities.items()}
    samples = {name: {metric: [] for metric in point} for name, point in points.items()
               if name not in frozen}
    delta_samples = {f"{left}_minus_{right}": {metric: [] for metric in points[left]}
                     for left, right in pairs}
    rng = np.random.default_rng(SEED)
    for _ in range(draws):
        multiplicity = np.bincount(rng.integers(0, len(students), len(students)),
                                  minlength=len(students))
        weight = multiplicity[cluster]
        current = {name: scores(y, p, weight, cluster) for name, p in probabilities.items()}
        for name, values in current.items():
            if name in frozen:
                continue
            for metric, value in values.items():
                samples[name][metric].append(value)
        for left, right in pairs:
            for metric in points[left]:
                a, b = current[left][metric], current[right][metric]
                delta_samples[f"{left}_minus_{right}"][metric].append(
                    a - b if a is not None and b is not None else None)
    output = {
        "status": "computed", "events": len(rows), "students": len(students),
        "questions": len({r["question_id"] for r in rows}),
        "correct": int(y.sum()), "incorrect": int(len(y) - y.sum()),
        "bootstrap": {"draws": draws, "seed": SEED, "unit": "base_student"},
        "models": {}, "paired_differences": {},
    }
    for name, point in points.items():
        if name in frozen:
            output["models"][name] = frozen[name]
            continue
        output["models"][name] = {
            **point, "auc_reason": None if point["auc"] is not None else "single_observed_class",
            "calibration": calibration_bins(y, probabilities[name]),
            "intervals": {metric: interval(values, len(students))
                          for metric, values in samples[name].items()},
        }
    for left, right in pairs:
        name = f"{left}_minus_{right}"
        output["paired_differences"][name] = {
            metric: {
                "estimate": points[left][metric] - points[right][metric]
                if points[left][metric] is not None and points[right][metric] is not None else None,
                "interval": interval(delta_samples[name][metric], len(students)),
            } for metric in points[left]
        }
    return output


def conformal_item_report(rows, calibration, draws=BOOTSTRAP_DRAWS):
    if not rows:
        return {"status": "no_eligible_targets", "events": 0}
    y = np.asarray([r["y"] for r in rows], dtype=int)
    p = np.asarray([r["p_history"] for r in rows], dtype=float)
    q0 = np.asarray([calibration["item_level"]["groups"][r["regime"]]["q_hat"]["0"] for r in rows])
    q1 = np.asarray([calibration["item_level"]["groups"][r["regime"]]["q_hat"]["1"] for r in rows])
    includes_zero, includes_one = p <= q0, (1 - p) <= q1
    coverage = np.where(y == 1, includes_one, includes_zero).astype(float)
    size = includes_zero.astype(int) + includes_one.astype(int)
    students, cluster = np.unique([r["student_code"] for r in rows], return_inverse=True)
    balanced = 1 / np.bincount(cluster)[cluster]
    samples, balanced_samples, rng = [], [], np.random.default_rng(SEED)
    for _ in range(draws):
        multiplicity = np.bincount(rng.integers(0, len(students), len(students)),
                                  minlength=len(students))
        weights = multiplicity[cluster]
        samples.append(float(np.dot(weights, coverage) / weights.sum()))
        balanced_weights = weights * balanced
        balanced_samples.append(float(np.dot(balanced_weights, coverage) / balanced_weights.sum()))
    result = {
        "events": len(rows), "students": len(students),
        "nominal_target": 1 - calibration["alpha"],
        "empirical_coverage": float(coverage.mean()),
        "student_balanced_coverage": float(np.dot(balanced, coverage) / balanced.sum()),
        "coverage_interval": interval(samples, len(students)),
        "student_balanced_coverage_interval": interval(balanced_samples, len(students)),
        "mean_set_size": float(size.mean()),
        "singleton_fraction": float(np.mean(size == 1)),
        "ambiguous_fraction": float(np.mean(size == 2)),
        "empty_fraction": float(np.mean(size == 0)),
        "interpretation": "Selected historical subset empirical coverage; no new coverage guarantee.",
    }
    return result
