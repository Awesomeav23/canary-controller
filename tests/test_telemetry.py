"""Controller metrics. Counters are process-global, so each test measures the change."""

import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from canary.api import create_app
from canary.config import Settings
from canary.models import Rollout, RolloutState


def sample(name: str, **labels) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


class Delta:
    """Snapshot a metric now; .value is how much it has grown since."""

    def __init__(self, name: str, **labels):
        self.name, self.labels = name, labels
        self.start = sample(name, **labels)

    @property
    def value(self) -> float:
        return sample(self.name, **self.labels) - self.start


def create(sessions, policy, app) -> int:
    with sessions.begin() as s:
        r = Rollout(app=app, image=f"{app}:v2", policy=policy.model_dump(), active_app=app)
        s.add(r)
        s.flush()
        return r.id


def test_success_records_outcome_and_duration(engine, sessions, clock, policy):
    finished = Delta("canary_rollouts_finished_total", app="t-ok", outcome="succeeded")
    duration_sum = Delta("canary_rollout_duration_seconds_sum", outcome="succeeded")
    duration_count = Delta("canary_rollout_duration_seconds_count", outcome="succeeded")
    healthy = Delta("canary_analyses_total", app="t-ok", result="healthy")

    create(sessions, policy, "t-ok")
    engine.tick()
    for _ in range(3):
        clock.advance(60)
        engine.tick()

    assert finished.value == 1
    assert healthy.value == 3
    assert duration_count.value == 1
    assert duration_sum.value == 180  # first traffic -> promoted


def test_time_to_rollback(engine, sessions, metrics, clock, policy):
    rolled_back = Delta("canary_rollouts_finished_total", app="t-bad", outcome="rolled_back")
    duration_sum = Delta("canary_rollout_duration_seconds_sum", outcome="rolled_back")
    unhealthy = Delta("canary_analyses_total", app="t-bad", result="unhealthy")

    create(sessions, policy, "t-bad")
    engine.tick()
    metrics.canary["error_rate"] = 0.5
    clock.advance(60)
    engine.tick()

    assert rolled_back.value == 1
    assert unhealthy.value == 1
    assert duration_sum.value == 60


def test_waiting_for_traffic_counted_once(engine, sessions, metrics, clock, policy):
    waiting = Delta("canary_analyses_total", app="t-wait", result="waiting_for_traffic")
    create(sessions, policy, "t-wait")
    engine.tick()
    metrics.canary["request_count"] = 1
    clock.advance(60)
    engine.tick()
    engine.tick()
    assert waiting.value == 1


def test_abort_while_pending_has_no_duration(engine, sessions, policy):
    aborted = Delta("canary_rollouts_finished_total", app="t-abort", outcome="aborted")
    duration_count = Delta("canary_rollout_duration_seconds_count", outcome="aborted")
    rid = create(sessions, policy, "t-abort")
    engine.abort(rid, "changed my mind")
    assert aborted.value == 1
    assert duration_count.value == 0  # the canary never ran


def test_failed_transaction_records_nothing(engine, sessions, router, clock, policy):
    """If the commit doesn't happen, neither does the metric."""
    finished = Delta("canary_rollouts_finished_total", app="t-fail", outcome="succeeded")
    errors = Delta("canary_advance_errors_total")
    rid = create(sessions, policy, "t-fail")
    engine.tick()
    for _ in range(2):
        clock.advance(60)
        engine.tick()

    def broken_promote(*args):
        raise RuntimeError("cluster unreachable")

    router.promote = broken_promote
    clock.advance(60)
    engine.tick()

    with sessions() as s:
        assert s.get(Rollout, rid).state == RolloutState.PROGRESSING
    assert finished.value == 0
    assert errors.value == 1


def test_active_rollouts_gauge(engine, sessions, policy):
    create(sessions, policy, "t-g1")
    create(sessions, policy, "t-g2")
    engine.tick()
    assert sample("canary_active_rollouts") == 2


@pytest.fixture
def client(sessions, router, metrics):
    app = create_app(Settings(), sessions=sessions, router=router, metrics=metrics, run_loop=False)
    with TestClient(app) as c:
        yield c


def test_metrics_endpoint(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    for name in ("canary_rollouts_finished_total", "canary_tick_duration_seconds"):
        assert name in resp.text
