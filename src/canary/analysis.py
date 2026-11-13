"""Decides whether a canary step is healthy. Pure functions: no I/O, easy to test."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from canary.policy import Threshold


@dataclass(frozen=True)
class Verdict:
    healthy: bool
    reasons: tuple[str, ...]


def evaluate(thresholds: Sequence[Threshold], readings: Mapping[str, float | None]) -> Verdict:
    reasons: list[str] = []
    for t in thresholds:
        value = readings.get(t.metric)
        if value is None:
            # Fail closed: if we can't see the canary, we can't call it healthy.
            reasons.append(f"{t.metric}: no data")
        elif value > t.max:
            reasons.append(f"{t.metric}={value:.4g} exceeds max {t.max:g}")
    return Verdict(healthy=not reasons, reasons=tuple(reasons))
