"""Rollout policy: the steps a canary walks through and the thresholds that gate each step."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

SUPPORTED_METRICS = frozenset({"error_rate", "p95_latency_ms"})


class Step(BaseModel):
    weight: int = Field(gt=0, le=100, description="Percent of replicas running the canary")
    pause_seconds: int = Field(ge=0, description="How long to bake before analysing")


class Threshold(BaseModel):
    """A health check on one canary metric. Set `max`, `max_over_stable`, or both.

    `max` is a hard ceiling (error_rate 0.05 = 5%). `max_over_stable` limits how much worse
    the canary may be than the current stable version, in the metric's own units
    (p95_latency_ms 100 = at most 100 ms slower than stable).
    """

    metric: str
    max: float | None = None
    max_over_stable: float | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _valid(self) -> "Threshold":
        if self.metric not in SUPPORTED_METRICS:
            raise ValueError(
                f"unknown metric {self.metric!r}; supported: {sorted(SUPPORTED_METRICS)}"
            )
        if self.max is None and self.max_over_stable is None:
            raise ValueError(f"{self.metric}: set max, max_over_stable, or both")
        return self


class Policy(BaseModel):
    total_replicas: int = Field(default=4, ge=2)
    analysis_window_seconds: int = Field(default=60, ge=10)
    # Don't judge a canary on a handful of requests: wait for this many in the window.
    min_requests: int = Field(default=50, ge=0)
    # How long past a step's pause to wait for min_requests before giving up (rolling back).
    max_wait_for_traffic_seconds: int = Field(default=120, ge=0)
    # How long canary pods get to become ready at each step before the rollout gives up
    # (a crash-looping or unpullable image never gets ready).
    pod_ready_timeout_seconds: int = Field(default=180, ge=10)
    steps: list[Step] = Field(min_length=1)
    thresholds: list[Threshold] = Field(min_length=1)

    @model_validator(mode="after")
    def _valid_steps(self) -> "Policy":
        weights = [s.weight for s in self.steps]
        if weights != sorted(set(weights)):
            raise ValueError("step weights must be strictly increasing")
        if weights[-1] != 100:
            raise ValueError("the last step must reach weight 100")
        return self


def load_policy(path: str | Path) -> Policy:
    return Policy.model_validate(yaml.safe_load(Path(path).read_text()))
