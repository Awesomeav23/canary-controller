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


# --- comparison against stable ---------------------------------------------

RELATIVE = [Threshold(metric="p95_latency_ms", max_over_stable=100)]


def test_within_allowance_vs_stable_is_healthy():
    assert evaluate(RELATIVE, {"p95_latency_ms": 180}, {"p95_latency_ms": 100}).healthy


def test_exactly_at_allowance_is_healthy():
    assert evaluate(RELATIVE, {"p95_latency_ms": 200}, {"p95_latency_ms": 100}).healthy


def test_over_allowance_vs_stable_is_unhealthy():
    v = evaluate(RELATIVE, {"p95_latency_ms": 260}, {"p95_latency_ms": 100})
    assert v.reasons == ("p95_latency_ms=260 is +160 vs stable (100), allowed +100",)


def test_canary_better_than_stable_is_healthy():
    assert evaluate(RELATIVE, {"p95_latency_ms": 20}, {"p95_latency_ms": 100}).healthy


@pytest.mark.parametrize("stable", [None, {}, {"p95_latency_ms": None}])
def test_no_baseline_fails_closed(stable):
    v = evaluate(RELATIVE, {"p95_latency_ms": 20}, stable)
    assert v.reasons == ("p95_latency_ms: no stable baseline to compare against",)


def test_both_checks_report_separately():
    both = [Threshold(metric="error_rate", max=0.05, max_over_stable=0.01)]
    v = evaluate(both, {"error_rate": 0.2}, {"error_rate": 0.0})
    assert len(v.reasons) == 2
