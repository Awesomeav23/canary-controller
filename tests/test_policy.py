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


def test_threshold_needs_max_or_max_over_stable():
    with pytest.raises(ValidationError, match="set max, max_over_stable, or both"):
        Policy.model_validate(base(thresholds=[{"metric": "error_rate"}]))


def test_relative_only_threshold_is_valid():
    p = Policy.model_validate(
        base(thresholds=[{"metric": "p95_latency_ms", "max_over_stable": 100}])
    )
    assert p.thresholds[0].max is None


def test_policies_saved_before_week_3_still_load():
    # Rollouts stored before min_requests existed must still parse (defaults apply).
    p = Policy.model_validate(base())
    assert p.min_requests == 50 and p.max_wait_for_traffic_seconds == 120
