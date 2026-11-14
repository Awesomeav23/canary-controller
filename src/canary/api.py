"""HTTP API. Run with: uvicorn --factory canary.api:create_app"""

import logging
import threading
from collections.abc import Iterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from canary.config import Settings
from canary.db import init_db, make_sessionmaker
from canary.engine import Engine, RolloutFinishedError
from canary.metrics import MetricsSource, PrometheusMetrics
from canary.models import DeployFreeze, Rollout, RolloutEvent
from canary.policy import Policy, load_policy
from canary.router import KubernetesRouter, LogRouter, TrafficRouter

log = logging.getLogger(__name__)

AppName = Annotated[str, Field(pattern=r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")]


class RolloutCreate(BaseModel):
    app: AppName
    image: str = Field(min_length=1, max_length=255)
    policy: Policy | None = None  # falls back to policies/default.yaml


class AbortRequest(BaseModel):
    reason: str = "aborted via API"


class FreezeRequest(BaseModel):
    reason: str = Field(min_length=1)


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    at: datetime
    kind: str
    message: str
    data: dict[str, Any] | None = None


class RolloutSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    app: str
    image: str
    state: str
    step_index: int
    current_weight: int
    reason: str | None
    created_at: datetime
    updated_at: datetime


class RolloutOut(RolloutSummary):
    policy: Policy
    baseline: dict[str, float | None] | None
    events: list[EventOut]


class FreezeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    app: str
    reason: str
    created_at: datetime


def build_router(settings: Settings) -> TrafficRouter:
    if settings.router == "kubernetes":
        return KubernetesRouter(settings.namespace)
    if settings.router == "log":
        return LogRouter()
    raise ValueError(f"unknown router {settings.router!r}")


def _run_loop(engine: Engine, stop: threading.Event, interval: float) -> None:
    while not stop.wait(interval):
        try:
            engine.tick()
        except Exception:
            log.exception("tick failed")


def create_app(
    settings: Settings | None = None,
    *,
    sessions: sessionmaker | None = None,
    router: TrafficRouter | None = None,
    metrics: MetricsSource | None = None,
    run_loop: bool = True,
) -> FastAPI:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    settings = settings or Settings.from_env()
    sessions = sessions or make_sessionmaker(settings.database_url)
    engine = Engine(
        sessions,
        router or build_router(settings),
        metrics or PrometheusMetrics(settings.prometheus_url),
    )
    default_policy = load_policy(settings.default_policy_path)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        init_db(sessions)
        stop = threading.Event()
        thread = None
        if run_loop:
            thread = threading.Thread(
                target=_run_loop,
                args=(engine, stop, settings.tick_seconds),
                name="rollout-loop",
                daemon=True,
            )
            thread.start()
        yield
        stop.set()
        if thread:
            thread.join(timeout=10)

    app = FastAPI(title="canary-controller", version="0.1.0", lifespan=lifespan)
    app.state.engine = engine

    def get_session() -> Iterator[Session]:
        with sessions() as s:
            yield s

    SessionDep = Annotated[Session, Depends(get_session)]

    def load(s: Session, rollout_id: int) -> Rollout:
        rollout = s.get(Rollout, rollout_id)
        if rollout is None:
            raise HTTPException(404, f"rollout {rollout_id} not found")
        return rollout

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/rollouts", status_code=201)
    def create_rollout(body: RolloutCreate, s: SessionDep) -> RolloutOut:
        policy = body.policy or default_policy
        rollout = Rollout(
            app=body.app, image=body.image, policy=policy.model_dump(), active_app=body.app
        )
        rollout.events.append(
            RolloutEvent(kind="created", message=f"rollout of {body.image} requested")
        )
        s.add(rollout)
        try:
            s.commit()
        except IntegrityError:
            s.rollback()
            raise HTTPException(409, f"{body.app} already has an active rollout") from None
        s.refresh(rollout)
        return RolloutOut.model_validate(rollout)

    @app.get("/rollouts")
    def list_rollouts(
        s: SessionDep, app_name: str | None = None, limit: int = 50
    ) -> list[RolloutSummary]:
        q = select(Rollout).order_by(Rollout.id.desc()).limit(min(limit, 200))
        if app_name:
            q = q.where(Rollout.app == app_name)
        return [RolloutSummary.model_validate(r) for r in s.scalars(q)]

    @app.get("/rollouts/{rollout_id}")
    def get_rollout(rollout_id: int, s: SessionDep) -> RolloutOut:
        return RolloutOut.model_validate(load(s, rollout_id))

    @app.post("/rollouts/{rollout_id}/abort")
    def abort_rollout(
        rollout_id: int, s: SessionDep, body: AbortRequest | None = None
    ) -> RolloutOut:
        try:
            engine.abort(rollout_id, (body or AbortRequest()).reason)
        except LookupError:
            raise HTTPException(404, f"rollout {rollout_id} not found") from None
        except RolloutFinishedError as e:
            raise HTTPException(409, f"rollout already finished ({e})") from None
        return RolloutOut.model_validate(load(s, rollout_id))

    @app.get("/freezes")
    def list_freezes(s: SessionDep) -> list[FreezeOut]:
        return [FreezeOut.model_validate(f) for f in s.scalars(select(DeployFreeze))]

    @app.put("/freezes/{app_name}")
    def freeze(app_name: AppName, body: FreezeRequest, s: SessionDep) -> FreezeOut:
        f = s.merge(DeployFreeze(app=app_name, reason=body.reason))
        s.commit()
        return FreezeOut.model_validate(f)

    @app.delete("/freezes/{app_name}", status_code=204)
    def unfreeze(app_name: str, s: SessionDep) -> Response:
        f = s.get(DeployFreeze, app_name)
        if f is None:
            raise HTTPException(404, f"no freeze for {app_name}")
        s.delete(f)
        s.commit()
        return Response(status_code=204)

    return app
