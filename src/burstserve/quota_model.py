"""Predict step latency from a compute quota, and score the prediction.

Gate B requires held-out solo p50 MAPE within 10%. That is a claim about
prediction, so it needs a model fitted on some quota points and scored on
points it never saw. Both halves have a way of being vacuously satisfied:

* a two-parameter model fitted on two points reproduces them exactly, so a
  MAPE computed without spare degrees of freedom means nothing;
* an empty held-out set has a MAPE of zero.

Both are refused here rather than reported as passes.

The functional form is Amdahl's: latency = serial + parallel / quota. It is
the shape the scheduler assumes, so fitting anything more flexible would
score a model the scheduler does not use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Sequence


class QuotaModelError(ValueError):
    """Raised when a fit or a score would not mean what it appears to."""


# Two free parameters, so three points is the first count that can disagree
# with the model at all.
MINIMUM_FIT_POINTS = 3


@dataclass(frozen=True)
class QuotaLatencyModel:
    """latency(q) = serial_s + parallel_s / q."""

    serial_s: float
    parallel_s: float
    fitted_quotas: tuple[int, ...]

    def predict(self, quota: int) -> float:
        if quota <= 0:
            raise QuotaModelError(f"quota must be positive, got {quota}")
        return self.serial_s + self.parallel_s / quota

    def extrapolates(self, quota: int) -> bool:
        """Whether this quota lies outside the range the fit ever saw.

        An extrapolated prediction can be accurate, but it is a different
        claim from an interpolated one and must not be reported as the same.
        """
        return not (min(self.fitted_quotas) <= quota <= max(self.fitted_quotas))


def fit_quota_latency(points: Iterable[tuple[int, float]]) -> QuotaLatencyModel:
    """Least squares of latency against 1/quota."""

    cleaned: list[tuple[int, float]] = []
    for quota, latency in points:
        if quota <= 0:
            raise QuotaModelError(f"quota must be positive, got {quota}")
        if latency <= 0:
            raise QuotaModelError(f"latency must be positive, got {latency}")
        cleaned.append((int(quota), float(latency)))

    distinct = {quota for quota, _ in cleaned}
    if len(distinct) < MINIMUM_FIT_POINTS:
        raise QuotaModelError(
            f"fitting two parameters needs at least {MINIMUM_FIT_POINTS} "
            f"distinct quotas, got {sorted(distinct)}"
        )

    xs = [1.0 / quota for quota, _ in cleaned]
    ys = [latency for _, latency in cleaned]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    variance = sum((x - mean_x) ** 2 for x in xs)
    if variance == 0:  # pragma: no cover - guarded by the distinct-quota check
        raise QuotaModelError("every point has the same quota")
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / variance
    intercept = mean_y - slope * mean_x
    return QuotaLatencyModel(
        serial_s=intercept,
        parallel_s=slope,
        fitted_quotas=tuple(sorted(distinct)),
    )


def mape(observed: Sequence[float], predicted: Sequence[float]) -> float:
    """Mean absolute percentage error, as a fraction rather than a percent."""
    if len(observed) != len(predicted):
        raise QuotaModelError("observed and predicted differ in length")
    if not observed:
        # An empty comparison has an error of zero, which would read as a
        # perfect score. It is an absent measurement, so it is refused.
        raise QuotaModelError("cannot score an empty comparison")
    total = 0.0
    for actual, guess in zip(observed, predicted):
        if actual == 0:
            raise QuotaModelError("cannot take a percentage error against zero")
        total += abs(guess - actual) / abs(actual)
    return total / len(observed)


def holdout_score(
    points: Sequence[tuple[int, float]],
    holdout_quotas: Sequence[int],
) -> dict:
    """Fit on everything except ``holdout_quotas`` and score on those.

    Returns the fit, the per-point errors, and whether each held-out quota
    was interpolated or extrapolated, because a gate that mixes the two is
    reporting a weaker claim than it appears to.
    """

    holdout = set(int(q) for q in holdout_quotas)
    if not holdout:
        raise QuotaModelError("an empty held-out set scores nothing")
    available = {int(q) for q, _ in points}
    missing = holdout - available
    if missing:
        raise QuotaModelError(f"held-out quotas were never measured: {sorted(missing)}")

    train = [(q, v) for q, v in points if int(q) not in holdout]
    test = [(q, v) for q, v in points if int(q) in holdout]
    model = fit_quota_latency(train)

    observed = [value for _, value in test]
    predicted = [model.predict(int(quota)) for quota, _ in test]
    errors = [
        {
            "quota": int(quota),
            "observed_s": value,
            "predicted_s": guess,
            "absolute_percentage_error": abs(guess - value) / abs(value),
            "extrapolated": model.extrapolates(int(quota)),
        }
        for (quota, value), guess in zip(test, predicted)
    ]
    return {
        "model": {
            "serial_s": model.serial_s,
            "parallel_s": model.parallel_s,
            "fitted_quotas": list(model.fitted_quotas),
        },
        "train_points": len(train),
        "holdout_points": len(test),
        "mape": mape(observed, predicted),
        "any_extrapolated": any(entry["extrapolated"] for entry in errors),
        "errors": errors,
    }
