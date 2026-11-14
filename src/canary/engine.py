"""The rollout state machine.

    pending --(no freeze)--> progressing --(all steps healthy)--> succeeded
                                  |
                                  +--(a step fails analysis)--> rolled_back
    pending/progressing --(abort API)--> aborted

All state lives in MySQL, so the controller can crash and resume. Each tick re-reads
the rollout, does at most one transition, and commits. Router calls are idempotent,
so a crash between "change the cluster" and "commit" is safe: the next tick repeats it.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from canary.analysis import evaluate
from canary.metrics import MetricsSource
from canary.models import ACTIVE_STATES, DeployFreeze, Rollout, RolloutEvent, RolloutState, utcnow
from canary.policy import Policy
from canary.router import TrafficRouter

log = logging.getLogger(__name__)


class RolloutFinishedError(Exception):
    pass


class Engine:
    def __init__(
        self,
        sessions: sessionmaker,
        router: TrafficRouter,
        metrics: MetricsSource,
        clock: Callable[[], datetime] = utcnow,
    ):
        self.sessions = sessions
        self.router = router
        self.metrics = metrics
        self.clock = clock

    def tick(self) -> None:
        """Advance every active rollout by at most one step."""
        with self.sessions() as s:
            ids = s.scalars(select(Rollout.id).where(Rollout.state.in_(ACTIVE_STATES))).all()
        for rollout_id in ids:
            try:
                self._advance_one(rollout_id)
            except Exception:
                # One broken rollout must not stall the others.
                log.exception("failed to advance rollout %s", rollout_id)

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

        step = policy.steps[r.step_index]
        waited = now - r.step_started_at
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
                return
            # Low traffic is not evidence of health. Give up rather than promote blind.
            self.router.rollback(r.app, policy.total_replicas)
            self._finish(s, r, RolloutState.ROLLED_BACK, f"insufficient traffic: {detail}")
            return

        canary = self._readings(policy, r.app, "canary")
        if self._requests(r.app, "stable", window) >= policy.min_requests:
            r.baseline = self._readings(policy, r.app, "stable")
        # else: keep the last trusted baseline (e.g. stable has no pods at the 100% step)

        verdict = evaluate(policy.thresholds, canary, r.baseline)
        summary = "healthy" if verdict.healthy else "; ".join(verdict.reasons)
        self._event(
            s,
            r,
            "analysis",
            f"step {r.step_index} ({step.weight}%): {summary}",
            {"canary": canary, "stable": r.baseline, "canary_requests": canary_requests},
        )

        if not verdict.healthy:
            self.router.rollback(r.app, policy.total_replicas)
            self._finish(s, r, RolloutState.ROLLED_BACK, "; ".join(verdict.reasons))
        elif r.step_index == len(policy.steps) - 1:
            self.router.promote(r.app, r.image, policy.total_replicas)
            self._finish(s, r, RolloutState.SUCCEEDED, "all steps healthy")
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
            if r.state == RolloutState.PROGRESSING:
                policy = Policy.model_validate(r.policy)
                self.router.rollback(r.app, policy.total_replicas)
            self._finish(s, r, RolloutState.ABORTED, reason)

    def _enter_step(self, s: Session, r: Rollout, policy: Policy, index: int, now: datetime):
        step = policy.steps[index]
        self.router.set_weight(r.app, r.image, step.weight, policy.total_replicas)
        r.step_index = index
        r.step_started_at = now
        self._event(s, r, "step", f"step {index}: canary at {step.weight}%")

    def _finish(self, s: Session, r: Rollout, state: RolloutState, reason: str) -> None:
        r.state = state
        r.reason = reason
        r.active_app = None  # frees the app for its next rollout
        self._event(s, r, state.value, reason)
        log.info("rollout %s (%s) %s: %s", r.id, r.app, state.value, reason)

    def _event(
        self, s: Session, r: Rollout, kind: str, message: str, data: dict[str, Any] | None = None
    ) -> None:
        r.events.append(RolloutEvent(at=self.clock(), kind=kind, message=message, data=data))
