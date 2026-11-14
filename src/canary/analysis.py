"""Decides whether a canary step is healthy. Pure functions: no I/O, easy to test."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from canary.policy import Threshold

Readings = Mapping[str, float | None]


@dataclass(frozen=True)
class Verdict:
    healthy: bool
    reasons: tuple[str, ...]


def evaluate(
    thresholds: Sequence[Threshold], canary: Readings, stable: Readings | None = None
) -> Verdict:
    """Check canary readings against each threshold.

    `stable` is the baseline to compare against (None if there isn't one). Missing data
    always counts as a failure: a canary we can't measure is not one we can trust.
    """
    reasons: list[str] = []
    for t in thresholds:
        value = canary.get(t.metric)
        if value is None:
            reasons.append(f"{t.metric}: no data")
            continue

        if t.max is not None and value > t.max:
            reasons.append(f"{t.metric}={value:.4g} exceeds max {t.max:g}")

        if t.max_over_stable is not None:
            base = stable.get(t.metric) if stable else None
            if base is None:
                reasons.append(f"{t.metric}: no stable baseline to compare against")
            elif value - base > t.max_over_stable:
                reasons.append(
                    f"{t.metric}={value:.4g} is {value - base:+.4g} vs stable ({base:.4g}),"
                    f" allowed +{t.max_over_stable:g}"
                )
    return Verdict(healthy=not reasons, reasons=tuple(reasons))
