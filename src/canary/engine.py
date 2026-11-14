"""The rollout state machine.

    pending --(no freeze)--> progressing --(all steps healthy)--> promoting --> succeeded
                                  |
                                  +--(a step fails, or pods never ready)--> rolled_back
    pending/progressing --(abort API)--> aborted

Each step: move traffic (once canary pods are ready) -> pause -> check traffic volume
-> analyse. `promoting` waits for stable to run the new image (GatewayRouter only;
ReplicaRouter promotes instantly).

All state lives in MySQL, so the controller can crash and resume. Each tick re-reads
the rollout, does at most one transition, and commits. Router calls are idempotent,
so a crash between "change the cluster" and "commit" is safe: the next tick repeats it.

Controller metrics (telemetry.py) are queued on the session and recorded only after the
commit succeeds, so a rolled-back transaction never reports an outcome that didn't happen.
"""

import logging
import time
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from canary.analysis import evaluate
from canary.metrics import MetricsSource
from canary.models import ACTIVE_STATES, DeployFreeze, Rollout, RolloutEvent, RolloutState, utcnow
from canary.notify import enqueue
from canary.policy import Policy, Step
from canary.router import TrafficRouter
from canary.telemetry import (
    ACTIVE_ROLLOUTS,
    ADVANCE_ERRORS,
    ANALYSES,
    ROLLOUT_DURATION,
    ROLLOUTS_FINISHED,
    TICK_DURATION,
)

log = logging.getLogger(__name__)


class RolloutFinishedError(Exception):
    pass


class RolloutBusyError(Exception):
    """The rollout is in a state that can't be interrupted safely (promoting)."""


class Engine:
    def __init__(
        self,
        sessions: sessionmaker,
        router: TrafficRouter,
        metrics: MetricsSource,
        clock: Callable[[], datetime] = utcnow,
        notify_sinks: Sequence[str] = (),
    ):
        self.sessions = sessions
        self.notify_sinks = tuple(notify_sinks)
        self.router = router
        self.metrics = metrics
        self.clock = clock

    def tick(self) -> None:
        """Advance every active rollout by at most one step."""
        started = time.perf_counter()
        with self.sessions() as s:
            ids = s.scalars(select(Rollout.id).where(Rollout.state.in_(ACTIVE_STATES))).all()
        ACTIVE_ROLLOUTS.set(len(ids))
        for rollout_id in ids:
            try:
                self._advance_one(rollout_id)
            except Exception:
                # One broken rollout must not stall the others.
                ADVANCE_ERRORS.inc()
                log.exception("failed to advance rollout %s", rollout_id)
        TICK_DURATION.observe(time.perf_counter() - started)

    def _advance_one(self, rollout_id: int) -> None:
        with self.sessions.begin() as s:
            # SKIP LOCKED: if another controller replica holds this row, leave it to them.
            rollout = s.scalars(
                select(Rollout)
                .where(Rollout.id == rollout_id, Rollout.state.in_(ACTIVE_STATES))
                .with_for_update(skip_locked=True)
            ).first()
            if rollout is not None:
                self._advance(s, rollout)
        self._record_telemetry(s)  # only reached if the commit succeeded

    def _advance(self, s: Session, r: Rollout) -> None:
        policy = Policy.model_validate(r.policy)
        now = self.clock()

        if r.state == RolloutState.PENDING:
            freeze = s.get(DeployFreeze, r.app)
            if freeze is not None:
                if not r.events or r.events[-1].kind != "blocked":
                    self._event(s, r, "blocked", f"deploy freeze active: {freeze.reason}")
                return
            r.state = RolloutState.PROGRESSING
            self._enter_step(s, r, policy, 0, now)
            return

        if r.state == RolloutState.PROMOTING:
            self._try_promote(s, r, policy)
            return

        step = policy.steps[r.step_index]
        if r.traffic_shifted_at is None:
            # Waiting for canary pods before this step's traffic can move.
            if self.router.set_weight(r.app, r.image, step.weight, policy.total_replicas):
                self._traffic_shifted(s, r, step, now)
            elif now - r.step_started_at >= timedelta(seconds=policy.pod_ready_timeout_seconds):
                self.router.rollback(r.app, policy.total_replicas)
                self._finish(
                    s,
                    r,
                    RolloutState.ROLLED_BACK,
                    f"canary pods not ready after {policy.pod_ready_timeout_seconds}s"
                    f" (step {r.step_index}, {step.weight}%)",
                )
            return

        waited = now - r.traffic_shifted_at
        if waited < timedelta(seconds=step.pause_seconds):
            return  # still baking

        window = policy.analysis_window_seconds
        canary_requests = self._requests(r.app, "canary", window)
        if canary_requests < policy.min_requests:
            limit = timedelta(seconds=step.pause_seconds + policy.max_wait_for_traffic_seconds)
            detail = (
                f"{canary_requests:.0f} canary requests in {window}s, need {policy.min_requests}"
            )
            if waited < limit:
                if r.events[-1].kind != "waiting":
                    self._event(
                        s, r, "waiting", f"step {r.step_index}: not enough traffic ({detail})"
                    )
                    self._queue(s, ANALYSES.labels(r.app, "waiting_for_traffic").inc)
                return
            # Low traffic is not evidence of health. Give up rather than promote blind.
            self.router.rollback(r.app, policy.total_replicas)
            self._finish(s, r, RolloutState.ROLLED_BACK, f"insufficient traffic: {detail}")
            return

        canary = self._readings(policy, r.app, "canary")
        self._refresh_baseline(r, policy)

        verdict = evaluate(policy.thresholds, canary, r.baseline)
        summary = "healthy" if verdict.healthy else "; ".join(verdict.reasons)
        self._event(
            s,
            r,
            "analysis",
            f"step {r.step_index} ({step.weight}%): {summary}",
            {"canary": canary, "stable": r.baseline, "canary_requests": canary_requests},
        )
        result = "healthy" if verdict.healthy else "unhealthy"
        self._queue(s, ANALYSES.labels(r.app, result).inc)

        if not verdict.healthy:
            self.router.rollback(r.app, policy.total_replicas)
            self._finish(s, r, RolloutState.ROLLED_BACK, "; ".join(verdict.reasons))
        elif r.step_index == len(policy.steps) - 1:
            self._try_promote(s, r, policy)
        else:
            self._enter_step(s, r, policy, r.step_index + 1, now)

    def _requests(self, app: str, track: str, window: int) -> float:
        return self.metrics.query("request_count", app, track, window) or 0.0

    def _readings(self, policy: Policy, app: str, track: str) -> dict[str, float | None]:
        window = policy.analysis_window_seconds
        return {
            t.metric: self.metrics.query(t.metric, app, track, window) for t in policy.thresholds
        }

    def abort(self, rollout_id: int, reason: str) -> None:
        with self.sessions.begin() as s:
            r = s.scalars(select(Rollout).where(Rollout.id == rollout_id).with_for_update()).first()
            if r is None:
                raise LookupError(rollout_id)
            if r.state not in ACTIVE_STATES:
                raise RolloutFinishedError(r.state)
            if r.state == RolloutState.PROMOTING:
                # Stable is mid-way through rolling to the new (already verified) image.
                # "Rolling back" now would mean a third version change, not a safe stop.
                raise RolloutBusyError("promotion in progress")
            if r.state == RolloutState.PROGRESSING:
                policy = Policy.model_validate(r.policy)
                self.router.rollback(r.app, policy.total_replicas)
            self._finish(s, r, RolloutState.ABORTED, reason)
        self._record_telemetry(s)

    def _refresh_baseline(self, r: Rollout, policy: Policy) -> None:
        """Save stable's readings if it has enough traffic to trust; else keep the last ones
        (stable gets no traffic at a 100% step)."""
        window = policy.analysis_window_seconds
        if self._requests(r.app, "stable", window) >= policy.min_requests:
            r.baseline = self._readings(policy, r.app, "stable")

    def _enter_step(self, s: Session, r: Rollout, policy: Policy, index: int, now: datetime):
        step = policy.steps[index]
        if index == 0:
            # Snapshot stable before any traffic moves, while it still serves everyone.
            # Without this, a policy whose first step is 100% never gets a baseline.
            self._refresh_baseline(r, policy)
        r.step_index = index
        r.step_started_at = now
        r.traffic_shifted_at = None
        if self.router.set_weight(r.app, r.image, step.weight, policy.total_replicas):
            self._traffic_shifted(s, r, step, now)
        else:
            self._event(
                s,
                r,
                "waiting_for_pods",
                f"step {index}: canary pods starting ({step.weight}% next)",
            )

    def _traffic_shifted(self, s: Session, r: Rollout, step: Step, now: datetime) -> None:
        r.traffic_shifted_at = now  # the step's pause counts from here
        self._event(s, r, "step", f"step {r.step_index}: canary at {step.weight}%")
        if r.started_at is None:
            r.started_at = now
            enqueue(s, r, "started", self.notify_sinks, now)

    def _try_promote(self, s: Session, r: Rollout, policy: Policy) -> None:
        if self.router.promote(r.app, r.image, policy.total_replicas):
            self._finish(s, r, RolloutState.SUCCEEDED, "all steps healthy")
        elif r.state != RolloutState.PROMOTING:
            r.state = RolloutState.PROMOTING
            self._event(s, r, "promoting", f"all steps healthy; rolling stable to {r.image}")

    def _finish(self, s: Session, r: Rollout, state: RolloutState, reason: str) -> None:
        r.state = state
        r.reason = reason
        r.active_app = None  # frees the app for its next rollout
        self._event(s, r, state.value, reason)
        log.info("rollout %s (%s) %s: %s", r.id, r.app, state.value, reason)
        enqueue(s, r, state.value, self.notify_sinks, self.clock())  # same transaction

        self._queue(s, ROLLOUTS_FINISHED.labels(r.app, state.value).inc)
        if r.started_at is not None:  # aborted while pending: the canary never ran
            seconds = (self.clock() - r.started_at).total_seconds()
            self._queue(s, lambda: ROLLOUT_DURATION.labels(state.value).observe(seconds))

    @staticmethod
    def _queue(s: Session, record: Callable[[], None]) -> None:
        s.info.setdefault("telemetry", []).append(record)

    @staticmethod
    def _record_telemetry(s: Session) -> None:
        for record in s.info.pop("telemetry", []):
            record()

    def _event(
        self, s: Session, r: Rollout, kind: str, message: str, data: dict[str, Any] | None = None
    ) -> None:
        r.events.append(RolloutEvent(at=self.clock(), kind=kind, message=message, data=data))
