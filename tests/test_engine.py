import pytest

from canary.engine import Engine, RolloutBusyError, RolloutFinishedError
from canary.models import DeployFreeze, Rollout, RolloutState
from canary.policy import Step


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

    metrics.canary["error_rate"] = 0.3
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
    metrics.canary["error_rate"] = None  # traffic is flowing, but the metric is missing
    clock.advance(60)
    engine.tick()
    assert get(sessions, rid).state == RolloutState.ROLLED_BACK


# --- Week 3: compare against stable ---------------------------------------


def test_canary_worse_than_stable_rolls_back_even_under_the_ceiling(
    engine, sessions, router, metrics, clock, policy
):
    rid = create(sessions, policy)
    engine.tick()

    metrics.stable["p95_latency_ms"] = 80
    metrics.canary["p95_latency_ms"] = 230  # under the 500 ms ceiling, but +150 vs stable
    clock.advance(60)
    engine.tick()

    r = get(sessions, rid)
    assert r.state == RolloutState.ROLLED_BACK
    assert "p95_latency_ms=230 is +150 vs stable (80), allowed +100" in r.reason
    assert router.calls[-1] == ("rollback", "demo", 4)


def test_small_regression_within_allowance_passes(engine, sessions, metrics, clock, policy):
    rid = create(sessions, policy)
    engine.tick()
    metrics.stable["p95_latency_ms"] = 80
    metrics.canary["p95_latency_ms"] = 150  # +70, allowance is +100
    clock.advance(60)
    engine.tick()
    assert get(sessions, rid).step_index == 1


def test_final_step_compares_against_last_saved_baseline(engine, sessions, metrics, clock, policy):
    rid = create(sessions, policy)
    engine.tick()
    metrics.stable["p95_latency_ms"] = 80
    clock.advance(60)
    engine.tick()  # step 0 -> 1, baseline saved
    clock.advance(60)
    engine.tick()  # step 1 -> 2 (100%)
    assert get(sessions, rid).baseline["p95_latency_ms"] == 80

    # At 100% stable has no pods, so no traffic and no readings.
    metrics.readings["stable"] = {"request_count": 0}
    metrics.canary["p95_latency_ms"] = 300  # +220 vs the saved baseline
    clock.advance(60)
    engine.tick()

    r = get(sessions, rid)
    assert r.state == RolloutState.ROLLED_BACK
    assert "vs stable (80)" in r.reason


def test_no_baseline_at_all_fails_closed(engine, sessions, metrics, clock, policy):
    metrics.readings["stable"] = {"request_count": 0}
    rid = create(sessions, policy)
    engine.tick()
    clock.advance(60)
    engine.tick()

    r = get(sessions, rid)
    assert r.state == RolloutState.ROLLED_BACK
    assert "no stable baseline" in r.reason


def test_low_stable_traffic_is_not_trusted_as_baseline(engine, sessions, metrics, clock, policy):
    metrics.stable.update(request_count=10, p95_latency_ms=5)  # too few requests to trust
    rid = create(sessions, policy)
    engine.tick()
    clock.advance(60)
    engine.tick()
    assert "no stable baseline" in get(sessions, rid).reason


# --- Week 3: minimum traffic ------------------------------------------------


def test_waits_for_traffic_then_continues(engine, sessions, router, metrics, clock, policy):
    rid = create(sessions, policy)
    engine.tick()
    metrics.canary["request_count"] = 12
    clock.advance(60)
    engine.tick()
    engine.tick()

    r = get(sessions, rid)
    assert r.state == RolloutState.PROGRESSING and r.step_index == 0
    assert [e.kind for e in r.events].count("waiting") == 1  # logged once, not every tick
    assert "12 canary requests in 60s, need 50" in r.events[-1].message

    metrics.canary["request_count"] = 400
    clock.advance(30)
    engine.tick()
    assert get(sessions, rid).step_index == 1


def test_never_enough_traffic_rolls_back(engine, sessions, router, metrics, clock, policy):
    rid = create(sessions, policy)
    engine.tick()
    metrics.canary["request_count"] = 3

    clock.advance(60 + 119)  # pause + just under max_wait_for_traffic_seconds
    engine.tick()
    assert get(sessions, rid).state == RolloutState.PROGRESSING

    clock.advance(1)
    engine.tick()
    r = get(sessions, rid)
    assert r.state == RolloutState.ROLLED_BACK
    assert r.reason.startswith("insufficient traffic")
    assert router.calls[-1] == ("rollback", "demo", 4)


def test_no_traffic_data_counts_as_zero(engine, sessions, metrics, clock, policy):
    rid = create(sessions, policy)
    engine.tick()
    metrics.canary["request_count"] = None
    clock.advance(60)
    engine.tick()
    assert get(sessions, rid).events[-1].kind == "waiting"


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


# --- Week 5: waiting for pods, promoting -------------------------------------


def test_pause_starts_when_traffic_moves_not_when_step_starts(
    engine, sessions, router, clock, policy
):
    router.canary_ready = False
    rid = create(sessions, policy)
    engine.tick()
    r = get(sessions, rid)
    assert r.state == RolloutState.PROGRESSING and r.traffic_shifted_at is None
    assert r.current_weight == 0
    assert r.events[-1].kind == "waiting_for_pods"

    clock.advance(90)  # pods take 90s; the 60s pause must not have been "used up"
    router.canary_ready = True
    engine.tick()
    r = get(sessions, rid)
    assert r.traffic_shifted_at == clock.now and r.current_weight == 25

    clock.advance(59)
    engine.tick()
    assert get(sessions, rid).step_index == 0  # still baking
    clock.advance(1)
    engine.tick()
    assert get(sessions, rid).step_index == 1


def test_canary_pods_never_ready_rolls_back(engine, sessions, router, clock, policy):
    router.canary_ready = False
    rid = create(sessions, policy)
    engine.tick()

    clock.advance(179)
    engine.tick()
    assert get(sessions, rid).state == RolloutState.PROGRESSING

    clock.advance(1)
    engine.tick()
    r = get(sessions, rid)
    assert r.state == RolloutState.ROLLED_BACK
    assert r.reason == "canary pods not ready after 180s (step 0, 25%)"
    assert router.calls[-1] == ("rollback", "demo", 4)
    assert r.started_at is None  # the canary never got traffic


def test_promotion_waits_for_stable_without_reanalysing(
    engine, sessions, router, metrics, clock, policy
):
    rid = create(sessions, policy)
    engine.tick()
    for _ in range(2):
        clock.advance(60)
        engine.tick()

    router.stable_ready = False
    clock.advance(60)
    engine.tick()
    r = get(sessions, rid)
    assert r.state == RolloutState.PROMOTING
    assert r.active_app == "demo"  # still blocks a second rollout
    analyses = [e.kind for e in r.events].count("analysis")

    metrics.canary["error_rate"] = 0.9  # analysis is over: must not be re-run
    for _ in range(3):
        clock.advance(10)
        engine.tick()
    r = get(sessions, rid)
    assert r.state == RolloutState.PROMOTING
    assert [e.kind for e in r.events].count("analysis") == analyses
    assert [e.kind for e in r.events].count("promoting") == 1

    router.stable_ready = True
    engine.tick()
    r = get(sessions, rid)
    assert r.state == RolloutState.SUCCEEDED and r.active_app is None


def test_cannot_abort_while_promoting(engine, sessions, router, clock, policy):
    rid = create(sessions, policy)
    engine.tick()
    for _ in range(2):
        clock.advance(60)
        engine.tick()
    router.stable_ready = False
    clock.advance(60)
    engine.tick()

    with pytest.raises(RolloutBusyError):
        engine.abort(rid, "too late")
    assert get(sessions, rid).state == RolloutState.PROMOTING


def test_restart_while_waiting_for_pods(sessions, router, metrics, clock, policy):
    router.canary_ready = False
    rid = create(sessions, policy)
    Engine(sessions, router, metrics, clock=clock).tick()

    router.canary_ready = True
    clock.advance(30)
    Engine(sessions, router, metrics, clock=clock).tick()  # a fresh controller
    r = get(sessions, rid)
    assert r.traffic_shifted_at == clock.now
    assert router.calls.count(("set_weight", "demo", "demo:v2", 25, 4)) == 2  # retried


def test_baseline_captured_before_first_traffic_shift(engine, sessions, metrics, clock, policy):
    """Found on kind: a one-step 100% policy never saw stable traffic, so it could never
    pass a max_over_stable check. Stable is now snapshotted before step 0."""
    one_step = policy.model_copy(update={"steps": [Step(weight=100, pause_seconds=20)]})
    metrics.stable["p95_latency_ms"] = 80
    rid = create(sessions, one_step)
    engine.tick()
    assert get(sessions, rid).baseline["p95_latency_ms"] == 80

    metrics.readings["stable"] = {"request_count": 0}  # 100% on canary now
    clock.advance(20)
    engine.tick()
    assert get(sessions, rid).state == RolloutState.SUCCEEDED
