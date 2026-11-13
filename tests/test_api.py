import pytest
from fastapi.testclient import TestClient

from canary.api import create_app
from canary.config import Settings


@pytest.fixture
def client(sessions, router, metrics):
    app = create_app(Settings(), sessions=sessions, router=router, metrics=metrics, run_loop=False)
    with TestClient(app) as c:
        yield c


def test_create_and_get_rollout(client):
    resp = client.post("/rollouts", json={"app": "demo", "image": "demo:v2"})
    assert resp.status_code == 201
    body = resp.json()
    assert body["state"] == "pending"
    assert body["policy"]["steps"][-1]["weight"] == 100  # default policy applied
    assert body["events"][0]["kind"] == "created"

    got = client.get(f"/rollouts/{body['id']}").json()
    assert got["image"] == "demo:v2"


def test_second_active_rollout_for_same_app_is_rejected(client):
    assert client.post("/rollouts", json={"app": "demo", "image": "demo:v2"}).status_code == 201
    resp = client.post("/rollouts", json={"app": "demo", "image": "demo:v3"})
    assert resp.status_code == 409
    # A different app is fine.
    assert client.post("/rollouts", json={"app": "other", "image": "x:1"}).status_code == 201


def test_app_can_roll_out_again_after_abort(client):
    rid = client.post("/rollouts", json={"app": "demo", "image": "demo:v2"}).json()["id"]
    resp = client.post(f"/rollouts/{rid}/abort", json={"reason": "wrong image"})
    assert resp.json()["state"] == "aborted"
    assert client.post(f"/rollouts/{rid}/abort").status_code == 409
    assert client.post("/rollouts", json={"app": "demo", "image": "demo:v3"}).status_code == 201


def test_invalid_input_is_rejected(client):
    bad_policy = {"steps": [{"weight": 50, "pause_seconds": 0}], "thresholds": []}
    assert (
        client.post(
            "/rollouts", json={"app": "demo", "image": "x", "policy": bad_policy}
        ).status_code
        == 422
    )
    assert client.post("/rollouts", json={"app": "Bad_Name", "image": "x"}).status_code == 422


def test_unknown_rollout_is_404(client):
    assert client.get("/rollouts/999").status_code == 404
    assert client.post("/rollouts/999/abort").status_code == 404


def test_freeze_lifecycle(client):
    assert client.put("/freezes/demo", json={"reason": "holiday"}).status_code == 200
    assert [f["app"] for f in client.get("/freezes").json()] == ["demo"]
    assert client.delete("/freezes/demo").status_code == 204
    assert client.delete("/freezes/demo").status_code == 404


def test_list_filters_by_app(client):
    client.post("/rollouts", json={"app": "a", "image": "a:1"})
    client.post("/rollouts", json={"app": "b", "image": "b:1"})
    assert [r["app"] for r in client.get("/rollouts", params={"app_name": "a"}).json()] == ["a"]
