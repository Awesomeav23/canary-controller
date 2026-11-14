"""Slack / webhook notifications, delivered from the outbox table with retries.

The engine never sends anything itself. It writes Notification rows in the same
transaction as the state change (enqueue), and the Dispatcher delivers them later:

  * a crash can't lose a notification (it's committed with the state change)
  * a Slack outage can't slow down or break a rollout
  * delivery is at least once: receivers dedupe on the delivery id
  * one row per sink, so retrying a failed Slack post never re-sends a webhook
"""

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from canary.models import Notification, NotificationStatus, Rollout, utcnow
from canary.telemetry import NOTIFICATIONS

log = logging.getLogger(__name__)

EVENTS = ("started", "succeeded", "rolled_back", "aborted")


class Sink(Protocol):
    def send(self, delivery_id: int, payload: Mapping[str, Any]) -> None:
        """Deliver or raise. Raising means: try again later."""
        ...


class WebhookSink:
    """POSTs the JSON payload. Receivers should dedupe on X-Canary-Delivery."""

    def __init__(
        self, url: str, timeout: float = 5.0, transport: httpx.BaseTransport | None = None
    ):
        self._url = url
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def send(self, delivery_id: int, payload: Mapping[str, Any]) -> None:
        resp = self._client.post(
            self._url,
            json={"delivery_id": delivery_id, **payload},
            headers={"X-Canary-Delivery": str(delivery_id)},
        )
        resp.raise_for_status()


class SlackSink:
    """Slack incoming webhook: https://api.slack.com/messaging/webhooks"""

    def __init__(
        self, url: str, timeout: float = 5.0, transport: httpx.BaseTransport | None = None
    ):
        self._url = url
        self._client = httpx.Client(timeout=timeout, transport=transport)

    def send(self, delivery_id: int, payload: Mapping[str, Any]) -> None:
        resp = self._client.post(self._url, json={"text": slack_text(payload)})
        resp.raise_for_status()


_SLACK = {
    "started": (":rocket:", "rollout started"),
    "succeeded": (":white_check_mark:", "promoted"),
    "rolled_back": (":rotating_light:", "rolled back"),
    "aborted": (":octagonal_sign:", "aborted"),
}


def slack_text(payload: Mapping[str, Any]) -> str:
    r = payload["rollout"]
    emoji, verb = _SLACK.get(payload["event"], (":grey_question:", payload["event"]))
    text = f"{emoji} *{r['app']}* `{r['image']}` {verb} (rollout {r['id']})"
    if r.get("reason") and payload["event"] != "succeeded":
        text += f"\n> {r['reason']}"
    return text


def enqueue(s: Session, r: Rollout, event: str, sinks: Sequence[str], now: datetime) -> None:
    """Add one outbox row per sink. Call inside the transaction that changes `r`."""
    payload = {
        "event": event,
        "at": now.isoformat() + "Z",
        "rollout": {
            "id": r.id,
            "app": r.app,
            "image": r.image,
            "state": r.state,
            "reason": r.reason,
            "weight": r.current_weight,
        },
    }
    for sink in sinks:
        s.add(
            Notification(
                rollout_id=r.id,
                event=event,
                sink=sink,
                payload=payload,
                status=NotificationStatus.PENDING,
                attempts=0,
                next_attempt_at=now,
                created_at=now,
            )
        )


class Dispatcher:
    def __init__(
        self,
        sessions: sessionmaker,
        sinks: Mapping[str, Sink],
        clock: Callable[[], datetime] = utcnow,
        max_attempts: int = 6,
        base_delay_seconds: float = 10,
        max_delay_seconds: float = 600,
    ):
        self.sessions = sessions
        self.sinks = sinks
        self.clock = clock
        self.max_attempts = max_attempts
        self.base_delay = base_delay_seconds
        self.max_delay = max_delay_seconds

    def dispatch_due(self, limit: int = 50) -> int:
        """Try every due notification once. Returns how many were sent."""
        with self.sessions() as s:
            ids = s.scalars(
                select(Notification.id)
                .where(
                    Notification.status == NotificationStatus.PENDING,
                    Notification.next_attempt_at <= self.clock(),
                )
                .order_by(Notification.id)
                .limit(limit)
            ).all()
        return sum(self._deliver(i) for i in ids)

    def _deliver(self, notification_id: int) -> bool:
        with self.sessions.begin() as s:
            # SKIP LOCKED: another controller replica may be delivering this one. The row
            # stays locked during the HTTP call, so sinks have a short timeout.
            n = s.scalars(
                select(Notification)
                .where(
                    Notification.id == notification_id,
                    Notification.status == NotificationStatus.PENDING,
                )
                .with_for_update(skip_locked=True)
            ).first()
            if n is None:
                return False
            now = self.clock()
            sink = self.sinks.get(n.sink)
            if sink is None:
                n.status, n.last_error = NotificationStatus.FAILED, "sink not configured"
                result = "failed"
            else:
                try:
                    sink.send(n.id, n.payload)
                    n.status, n.sent_at = NotificationStatus.SENT, now
                    result = "sent"
                except Exception as e:  # any failure is retryable until max_attempts
                    n.attempts += 1
                    n.last_error = f"{type(e).__name__}: {e}"[:500]
                    if n.attempts >= self.max_attempts:
                        n.status, result = NotificationStatus.FAILED, "failed"
                        log.error("notification %s to %s gave up: %s", n.id, n.sink, n.last_error)
                    else:
                        delay = min(self.base_delay * 2 ** (n.attempts - 1), self.max_delay)
                        n.next_attempt_at = now + timedelta(seconds=delay)
                        result = "retry"
            sink_name = n.sink
        NOTIFICATIONS.labels(sink_name, result).inc()  # after commit, like telemetry.py
        return result == "sent"
