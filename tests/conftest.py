import os
from datetime import datetime, timedelta

import pytest

from canary.db import drop_db, init_db, make_sessionmaker
from canary.engine import Engine
from canary.policy import Policy, Step, Threshold


class FakeRouter:
    def __init__(self):
        self.calls: list[tuple] = []
        self.fail_for: set[str] = set()

    def _record(self, *call):
        if call[1] in self.fail_for:
            raise RuntimeError(f"cluster unreachable for {call[1]}")
        self.calls.append(call)

    def set_weight(self, app, image, weight, total):
        self._record("set_weight", app, image, weight, total)

    def promote(self, app, image, total):
        self._record("promote", app, image, total)

    def rollback(self, app, total):
        self._record("rollback", app, total)


class FakeMetrics:
    def __init__(self):
        self.readings: dict[str, float | None] = {"error_rate": 0.0}

    def query(self, metric, app, track, window_seconds):
        return self.readings.get(metric)


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, 12, 0, 0)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def sessions():
    # Defaults to in-memory SQLite. CI (and `make test-mysql`) point this at real MySQL.
    # `or`, not a getenv default: CI sets the variable to "" for the SQLite job.
    sm = make_sessionmaker(os.getenv("CANARY_TEST_DATABASE_URL") or "sqlite://")
    drop_db(sm)
    init_db(sm)
    yield sm
    drop_db(sm)
    sm.kw["bind"].dispose()


@pytest.fixture
def router():
    return FakeRouter()


@pytest.fixture
def metrics():
    return FakeMetrics()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def engine(sessions, router, metrics, clock):
    return Engine(sessions, router, metrics, clock=clock)


@pytest.fixture
def policy():
    return Policy(
        total_replicas=4,
        analysis_window_seconds=60,
        steps=[
            Step(weight=25, pause_seconds=60),
            Step(weight=50, pause_seconds=60),
            Step(weight=100, pause_seconds=60),
        ],
        thresholds=[Threshold(metric="error_rate", max=0.05)],
    )
