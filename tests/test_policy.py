import pytest
from pydantic import ValidationError

from canary.config import Settings
from canary.policy import Policy, load_policy


def base(**overrides):
    d = {
        "steps": [{"weight": 50, "pause_seconds": 10}, {"weight": 100, "pause_seconds": 10}],
        "thresholds": [{"metric": "error_rate", "max": 0.05}],
    }
    return {**d, **overrides}


def test_default_policy_file_is_valid():
    policy = load_policy(Settings().default_policy_path)
    assert policy.steps[-1].weight == 100


def test_valid_policy():
    assert Policy.model_validate(base()).total_replicas == 4


@pytest.mark.parametrize(
    "steps",
    [
        [{"weight": 50, "pause_seconds": 0}, {"weight": 25, "pause_seconds": 0}],
        [{"weight": 50, "pause_seconds": 0}, {"weight": 50, "pause_seconds": 0}],
    ],
)
def test_weights_must_strictly_increase(steps):
    with pytest.raises(ValidationError, match="strictly increasing"):
        Policy.model_validate(base(steps=steps + [{"weight": 100, "pause_seconds": 0}]))


def test_last_step_must_reach_100():
    with pytest.raises(ValidationError, match="reach weight 100"):
        Policy.model_validate(base(steps=[{"weight": 50, "pause_seconds": 0}]))


def test_unknown_metric_rejected():
    with pytest.raises(ValidationError, match="unknown metric"):
        Policy.model_validate(base(thresholds=[{"metric": "cpu", "max": 1}]))
