import pytest

from canary.engine import Engine, RolloutFinishedError
from canary.models import DeployFreeze, Rollout, RolloutState


def create(sessions, policy, app="demo", image="demo:v2") -> int:
    with sessions.begin() as s:
        r = Rollout(app=app, image=image, policy=policy.model_dump(), active_app=app)
        s.add(r)
        s.flush()
        return r.id


def get(sessions, rollout_id) -> Rollout:
    with sessions() as s:
        r = s.get(Rollout, rollout_id)
        _ = r.events  # load before the session closes
        return r


def test_happy_path_walks_every_step_then_promotes(engine, sessions, router, clock, policy):
    rid = create(sessions, policy)

    engine.tick()
    assert get(sessions, rid).state == RolloutState.PROGRESSING
    assert router.calls == [("set_weight", "demo", "demo:v2", 25, 4)]

    engine.tick()  # pause not over: nothing happens
    assert len(router.calls) == 1

    clock.advance(60)
    engine.tick()
    assert router.calls[-1] == ("set_weight", "demo", "demo:v2", 50, 4)

    clock.advance(60)
    engine.tick()
    assert router.calls[-1] == ("set_weight", "demo", "demo:v2", 100, 4)

    clock.advance(60)
    engine.tick()
    r = get(sessions, rid)
    assert r.state == RolloutState.SUCCEEDED
    assert r.current_weight == 100
    assert r.active_app is None
    assert router.calls[-1] == ("promote", "demo", "demo:v2", 4)


def test_bad_metrics_roll_back(engine, sessions, router, metrics, clock, policy):
    rid = create(sessions, policy)
    engine.tick()

    metrics.readings["error_rate"] = 0.3
    clock.advance(60)
    engine.tick()

    r = get(sessions, rid)
    assert r.state == RolloutState.ROLLED_BACK
    assert "error_rate=0.3 exceeds max 0.05" in r.reason
    assert router.calls[-1] == ("rollback", "demo", 4)
    assert r.active_app is None
    assert [e.kind for e in r.events][-2:] == ["analysis", "rolled_back"]


def test_missing_metrics_roll_back(engine, sessions, metrics, clock, policy):
    rid = create(sessions, policy)
    engine.tick()
    metrics.readings.clear()
    clock.advance(60)
    engine.tick()
    assert get(sessions, rid).state == RolloutState.ROLLED_BACK


def test_freeze_holds_pending_rollout(engine, sessions, router, policy):
    with sessions.begin() as s:
        s.add(DeployFreeze(app="demo", reason="incident in progress"))
    rid = create(sessions, policy)

    engine.tick()
    engine.tick()
    r = get(sessions, rid)
    assert r.state == RolloutState.PENDING
    assert router.calls == []
    assert [e.kind for e in r.events] == ["blocked"]  # logged once, not every tick

    with sessions.begin() as s:
        s.delete(s.get(DeployFreeze, "demo"))
    engine.tick()
    assert get(sessions, rid).state == RolloutState.PROGRESSING


def test_abort_rolls_back_and_frees_app(engine, sessions, router, policy):
    rid = create(sessions, policy)
    engine.tick()

    engine.abort(rid, "bad vibes")
    r = get(sessions, rid)
    assert r.state == RolloutState.ABORTED
    assert router.calls[-1] == ("rollback", "demo", 4)

    with pytest.raises(RolloutFinishedError):
        engine.abort(rid, "again")


def test_resumes_after_controller_restart(sessions, router, metrics, clock, policy):
    rid = create(sessions, policy)
    Engine(sessions, router, metrics, clock=clock).tick()

    # The first controller "crashes"; a new one picks up from the database.
    clock.advance(60)
    Engine(sessions, router, metrics, clock=clock).tick()

    r = get(sessions, rid)
    assert r.step_index == 1
    assert router.calls[-1] == ("set_weight", "demo", "demo:v2", 50, 4)


def test_one_failing_rollout_does_not_block_others(engine, sessions, router, policy):
    broken = create(sessions, policy, app="broken")
    healthy = create(sessions, policy, app="healthy")
    router.fail_for.add("broken")

    engine.tick()

    assert get(sessions, broken).state == RolloutState.PENDING  # transaction rolled back
    assert get(sessions, healthy).state == RolloutState.PROGRESSING
