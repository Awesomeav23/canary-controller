"""Rollout policy: the steps a canary walks through and the thresholds that gate each step."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

SUPPORTED_METRICS = frozenset({"error_rate", "p95_latency_ms"})


class Step(BaseModel):
    weight: int = Field(gt=0, le=100, description="Percent of replicas running the canary")
    pause_seconds: int = Field(ge=0, description="How long to bake before analysing")


class Threshold(BaseModel):
    metric: str
    max: float

    @model_validator(mode="after")
    def _known_metric(self) -> "Threshold":
        if self.metric not in SUPPORTED_METRICS:
            raise ValueError(
                f"unknown metric {self.metric!r}; supported: {sorted(SUPPORTED_METRICS)}"
            )
        return self


class Policy(BaseModel):
    total_replicas: int = Field(default=4, ge=2)
    analysis_window_seconds: int = Field(default=60, ge=10)
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
