"""Notifications: written in the rollout's transaction, delivered by the Dispatcher."""

import json

import httpx
import pytest
from sqlalchemy import select

from canary.engine import Engine
from canary.models import Notification, Rollout
from canary.notify import Dispatcher, SlackSink, WebhookSink, slack_text

SINKS = ("slack", "webhook-1")


class FakeSink:
    def __init__(self, fail_times: int = 0):
        self.sent: list[tuple[int, dict]] = []
        self.fail_times = fail_times

    def send(self, delivery_id, payload):
        if self.fail_times:
            self.fail_times -= 1
            raise ConnectionError("sink down")
        self.sent.append((delivery_id, payload))


@pytest.fixture
def notifying_engine(sessions, router, metrics, clock):
    return Engine(sessions, router, metrics, clock=clock, notify_sinks=SINKS)


def create(sessions, policy, app="demo") -> int:
    with sessions.begin() as s:
        r = Rollout(app=app, image=f"{app}:v2", policy=policy.model_dump(), active_app=app)
        s.add(r)
        s.flush()
        return r.id


def rows(sessions) -> list[Notification]:
    with sessions() as s:
        return list(s.scalars(select(Notification).order_by(Notification.id)))


def run_to_rollback(engine, sessions, metrics, clock, policy) -> int:
    rid = create(sessions, policy)
    engine.tick()
    metrics.canary["error_rate"] = 0.5
    clock.advance(60)
    engine.tick()
    return rid


# --- enqueue -------------------------------------------------------------------


def test_started_and_rolled_back_enqueued_once_per_sink(
    notifying_engine, sessions, metrics, clock, policy
):
    run_to_rollback(notifying_engine, sessions, metrics, clock, policy)
    assert [(n.event, n.sink, n.status) for n in rows(sessions)] == [
        ("started", "slack", "pending"),
        ("started", "webhook-1", "pending"),
        ("rolled_back", "slack", "pending"),
        ("rolled_back", "webhook-1", "pending"),
    ]
    payload = rows(sessions)[-1].payload
    assert payload["rollout"]["state"] == "rolled_back"
    assert "error_rate=0.5" in payload["rollout"]["reason"]


def test_success_enqueues_started_and_succeeded(notifying_engine, sessions, clock, policy):
    create(sessions, policy)
    notifying_engine.tick()
    for _ in range(3):
        clock.advance(60)
        notifying_engine.tick()
    assert [n.event for n in rows(sessions) if n.sink == "slack"] == ["started", "succeeded"]


def test_no_sinks_no_rows(engine, sessions, metrics, clock, policy):
    run_to_rollback(engine, sessions, metrics, clock, policy)
    assert rows(sessions) == []


def test_failed_transaction_enqueues_nothing(notifying_engine, sessions, router, clock, policy):
    router.fail_for.add("demo")  # set_weight raises: the step 0 transaction rolls back
    create(sessions, policy)
    notifying_engine.tick()
    assert rows(sessions) == []


# --- dispatch ------------------------------------------------------------------


def test_dispatch_delivers_in_order_and_marks_sent(
    notifying_engine, sessions, metrics, clock, policy
):
    run_to_rollback(notifying_engine, sessions, metrics, clock, policy)
    slack, hook = FakeSink(), FakeSink()
    sent = Dispatcher(sessions, {"slack": slack, "webhook-1": hook}, clock=clock).dispatch_due()

    assert sent == 4
    assert [p["event"] for _, p in slack.sent] == ["started", "rolled_back"]
    assert all(n.status == "sent" and n.sent_at == clock.now for n in rows(sessions))
    # Second run sends nothing: delivered rows are done.
    assert (
        Dispatcher(sessions, {"slack": slack, "webhook-1": hook}, clock=clock).dispatch_due() == 0
    )


def test_one_sink_down_does_not_resend_to_the_other(
    notifying_engine, sessions, metrics, clock, policy
):
    run_to_rollback(notifying_engine, sessions, metrics, clock, policy)
    slack, hook = FakeSink(fail_times=99), FakeSink()
    d = Dispatcher(sessions, {"slack": slack, "webhook-1": hook}, clock=clock)
    d.dispatch_due()
    clock.advance(3600)
    d.dispatch_due()
    assert len(hook.sent) == 2  # exactly once each, despite slack retrying


def test_retries_with_exponential_backoff_then_succeeds(
    notifying_engine, sessions, metrics, clock, policy
):
    create(sessions, policy)
    notifying_engine.tick()  # one "started" per sink
    slack = FakeSink(fail_times=2)
    d = Dispatcher(sessions, {"slack": slack, "webhook-1": FakeSink()}, clock=clock)

    d.dispatch_due()  # attempt 1 fails -> next in 10s
    n = [n for n in rows(sessions) if n.sink == "slack"][0]
    assert (n.status, n.attempts, n.last_error) == ("pending", 1, "ConnectionError: sink down")
    assert (n.next_attempt_at - clock.now).total_seconds() == 10

    clock.advance(9)
    d.dispatch_due()  # not due yet
    assert slack.sent == []

    clock.advance(1)
    d.dispatch_due()  # attempt 2 fails -> next in 20s
    n = [n for n in rows(sessions) if n.sink == "slack"][0]
    assert (n.next_attempt_at - clock.now).total_seconds() == 20

    clock.advance(20)
    d.dispatch_due()  # attempt 3 succeeds
    assert len(slack.sent) == 1
    assert [n for n in rows(sessions) if n.sink == "slack"][0].status == "sent"


def test_gives_up_after_max_attempts(notifying_engine, sessions, clock, policy):
    create(sessions, policy)
    notifying_engine.tick()
    d = Dispatcher(
        sessions,
        {"slack": FakeSink(fail_times=99), "webhook-1": FakeSink()},
        clock=clock,
        max_attempts=3,
    )
    for _ in range(5):
        d.dispatch_due()
        clock.advance(3600)
    n = [n for n in rows(sessions) if n.sink == "slack"][0]
    assert (n.status, n.attempts) == ("failed", 3)


def test_backoff_is_capped(notifying_engine, sessions, clock, policy):
    create(sessions, policy)
    notifying_engine.tick()
    d = Dispatcher(
        sessions,
        {"slack": FakeSink(fail_times=99), "webhook-1": FakeSink()},
        clock=clock,
        max_attempts=20,
        max_delay_seconds=600,
    )
    for _ in range(10):
        d.dispatch_due()
        clock.advance(3600)
    d.dispatch_due()
    n = [n for n in rows(sessions) if n.sink == "slack"][0]
    assert (n.next_attempt_at - clock.now).total_seconds() == 600


def test_unconfigured_sink_is_failed_not_retried_forever(notifying_engine, sessions, clock, policy):
    create(sessions, policy)
    notifying_engine.tick()
    Dispatcher(sessions, {"webhook-1": FakeSink()}, clock=clock).dispatch_due()
    n = [n for n in rows(sessions) if n.sink == "slack"][0]
    assert (n.status, n.last_error) == ("failed", "sink not configured")


# --- sinks ---------------------------------------------------------------------

PAYLOAD = {
    "event": "rolled_back",
    "at": "2026-01-01T00:00:00Z",
    "rollout": {
        "id": 7,
        "app": "demo",
        "image": "demo-app:v2-bad",
        "state": "rolled_back",
        "reason": "error_rate=0.3 exceeds max 0.05",
        "weight": 0,
    },
}


def capture(status=200):
    seen: list[httpx.Request] = []

    def handler(request):
        seen.append(request)
        return httpx.Response(status)

    return seen, httpx.MockTransport(handler)


def test_webhook_sends_payload_with_delivery_id():
    seen, transport = capture()
    WebhookSink("https://hooks.example.com/x", transport=transport).send(42, PAYLOAD)
    req = seen[0]
    assert req.headers["X-Canary-Delivery"] == "42"
    body = json.loads(req.content)
    assert body["delivery_id"] == 42 and body["rollout"]["app"] == "demo"


def test_webhook_error_status_raises_so_it_retries():
    _, transport = capture(status=503)
    with pytest.raises(httpx.HTTPStatusError):
        WebhookSink("https://hooks.example.com/x", transport=transport).send(1, PAYLOAD)


def test_slack_message():
    seen, transport = capture()
    SlackSink("https://hooks.slack.com/services/x", transport=transport).send(1, PAYLOAD)
    assert json.loads(seen[0].content) == {
        "text": ":rotating_light: *demo* `demo-app:v2-bad` rolled back (rollout 7)\n"
        "> error_rate=0.3 exceeds max 0.05"
    }


def test_slack_success_has_no_reason_line():
    ok = {
        **PAYLOAD,
        "event": "succeeded",
        "rollout": {**PAYLOAD["rollout"], "reason": "all steps healthy"},
    }
    assert slack_text(ok) == ":white_check_mark: *demo* `demo-app:v2-bad` promoted (rollout 7)"
