import pytest

from canary.analysis import evaluate
from canary.policy import Threshold
from canary.router import split_replicas

THRESHOLDS = [Threshold(metric="error_rate", max=0.05), Threshold(metric="p95_latency_ms", max=500)]


def test_healthy_when_all_under_threshold():
    v = evaluate(THRESHOLDS, {"error_rate": 0.01, "p95_latency_ms": 120})
    assert v.healthy and v.reasons == ()


def test_unhealthy_when_threshold_exceeded():
    v = evaluate(THRESHOLDS, {"error_rate": 0.2, "p95_latency_ms": 120})
    assert not v.healthy
    assert "error_rate=0.2 exceeds max 0.05" in v.reasons


def test_missing_data_fails_closed():
    v = evaluate(THRESHOLDS, {"error_rate": 0.0, "p95_latency_ms": None})
    assert not v.healthy
    assert v.reasons == ("p95_latency_ms: no data",)


@pytest.mark.parametrize(
    "weight,total,expected",
    [
        (0, 4, (4, 0)),
        (25, 4, (3, 1)),
        (50, 4, (2, 2)),
        (100, 4, (0, 4)),
        (1, 4, (3, 1)),  # tiny weights still get one canary pod
        (99, 4, (1, 3)),  # and stable keeps one until 100%
    ],
)
def test_split_replicas(weight, total, expected):
    assert split_replicas(weight, total) == expected
