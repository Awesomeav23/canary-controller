from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# MySQL DATETIME drops sub-second precision unless asked; we compare step timestamps.
Timestamp = DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class RolloutState(StrEnum):
    PENDING = "pending"
    PROGRESSING = "progressing"
    SUCCEEDED = "succeeded"
    ROLLED_BACK = "rolled_back"
    ABORTED = "aborted"


ACTIVE_STATES = (RolloutState.PENDING, RolloutState.PROGRESSING)


class Rollout(Base):
    __tablename__ = "rollouts"

    id: Mapped[int] = mapped_column(primary_key=True)
    app: Mapped[str] = mapped_column(String(63), index=True)
    image: Mapped[str] = mapped_column(String(255))
    policy: Mapped[dict[str, Any]] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(20), default=RolloutState.PENDING, index=True)
    step_index: Mapped[int] = mapped_column(default=-1)
    step_started_at: Mapped[datetime | None] = mapped_column(Timestamp)
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
        if self.state == RolloutState.SUCCEEDED:
            return 100
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
