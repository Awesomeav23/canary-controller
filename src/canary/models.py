from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, MetaData, String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# MySQL DATETIME drops sub-second precision unless asked; we compare step timestamps.
Timestamp = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    # Explicit constraint names, so migrations can refer to them and they are the same
    # on MySQL and SQLite (otherwise each database invents its own).
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class RolloutState(StrEnum):
    PENDING = "pending"
    PROGRESSING = "progressing"
    PROMOTING = "promoting"  # passed every step; waiting for stable to run the new image
    SUCCEEDED = "succeeded"
    ROLLED_BACK = "rolled_back"
    ABORTED = "aborted"


ACTIVE_STATES = (RolloutState.PENDING, RolloutState.PROGRESSING, RolloutState.PROMOTING)


class Rollout(Base):
    __tablename__ = "rollouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    app: Mapped[str] = mapped_column(String(63), index=True)
    image: Mapped[str] = mapped_column(String(255))
    policy: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20), default=RolloutState.PENDING, index=True)
    step_index: Mapped[int] = mapped_column(default=-1)
    step_started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    # When this step's traffic actually moved. NULL while waiting for canary pods to be
    # ready; the step's pause only starts counting from here.
    traffic_shifted_at: Mapped[datetime | None] = mapped_column(Timestamp)
    # When the canary first got traffic (step 0). Rollout duration and time-to-rollback
    # are measured from here, not from creation, so freeze/queue time doesn't count.
    started_at: Mapped[datetime | None] = mapped_column(Timestamp)
    # Last stable readings with enough traffic to trust. At the 100% step stable has no
    # pods, so the canary is compared against this instead.
    baseline: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    # Holds the app name while the rollout is active and NULL once it finishes. The unique
    # index lets the database (not app code) guarantee one active rollout per app, even
    # when two API requests race.
    active_app: Mapped[str | None] = mapped_column(String(63), unique=True)
    reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow, onupdate=utcnow)

    events: Mapped[list[RolloutEvent]] = relationship(
        back_populates="rollout", order_by="RolloutEvent.id", cascade="all, delete-orphan"
    )

    @property
    def current_weight(self) -> int:
        if self.state in (RolloutState.SUCCEEDED, RolloutState.PROMOTING):
            return 100
        if self.state == RolloutState.PROGRESSING and self.traffic_shifted_at is None:
            return (
                int(self.policy["steps"][self.step_index - 1]["weight"]) if self.step_index else 0
            )
        if self.state == RolloutState.PROGRESSING and self.step_index >= 0:
            return int(self.policy["steps"][self.step_index]["weight"])
        return 0


class RolloutEvent(Base):
    """Append-only audit log of everything the controller did to a rollout."""

    __tablename__ = "rollout_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    rollout_id: Mapped[int] = mapped_column(ForeignKey("rollouts.id"), index=True)
    at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)
    kind: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any] | None] = mapped_column(JSON)

    rollout: Mapped[Rollout] = relationship(back_populates="events")


class DeployFreeze(Base):
    """While a freeze exists for an app, new rollouts stay pending."""

    __tablename__ = "deploy_freezes"

    app: Mapped[str] = mapped_column(String(63), primary_key=True)
    reason: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp, default=utcnow)


class NotificationStatus(StrEnum):
    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"  # gave up after max attempts, or the sink is no longer configured


class Notification(Base):
    """Transactional outbox: one row per (event, sink), written in the same transaction
    as the state change it describes, then delivered with retries by notify.Dispatcher.
    A crash can't lose a notification, and a sink outage can't block a rollout."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notifications_due", "status", "next_attempt_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)  # also the delivery id receivers dedupe on
    rollout_id: Mapped[int] = mapped_column(ForeignKey("rollouts.id"), index=True)
    event: Mapped[str] = mapped_column(String(20))  # started | succeeded | rolled_back | aborted
    sink: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(10), default=NotificationStatus.PENDING)
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(Timestamp)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(Timestamp)
    sent_at: Mapped[datetime | None] = mapped_column(Timestamp)
