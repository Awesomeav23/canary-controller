"""canary-rollout against a scripted fake API."""

import json

import httpx
import pytest

from canary.cli import EXIT_FAILED, EXIT_SUCCEEDED, EXIT_UNKNOWN, main

ARGS = ["--api", "http://canary.test/", "--app", "demo", "--image", "demo-app:v2", "--poll", "1"]


def rollout(state, events=("created",), reason=None):
    return {
        "id": 7,
        "state": state,
        "reason": reason,
        "events": [{"at": "2026-01-01T00:00:00", "kind": k, "message": k} for k in events],
    }


class FakeApi:
    """POST returns `created`; each GET returns the next item of `polls`."""

    def __init__(self, polls, created=None, post_status=201):
        self.polls = list(polls)
        self.created = created or rollout("pending")
        self.post_status = post_status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "POST" and request.url.path == "/rollouts":
            return httpx.Response(self.post_status, json=self.created)
        if request.method == "POST":
            return httpx.Response(200, json=rollout("aborted"))
        item = self.polls.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, int):
            return httpx.Response(item)
        return httpx.Response(200, json=item)


class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def monotonic(self):
        return self.t


def run(api: FakeApi, args=ARGS, clock=None):
    clock = clock or Clock()
    return main(
        args, transport=httpx.MockTransport(api), sleep=clock.sleep, monotonic=clock.monotonic
    )


@pytest.fixture(autouse=True)
def outputs(tmp_path, monkeypatch):
    path = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    monkeypatch.delenv("CANARY_API_TOKEN", raising=False)
    return path


def read_outputs(path) -> dict[str, str]:
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


def test_succeeded_exits_0_and_writes_outputs(outputs, capsys):
    api = FakeApi(
        [
            rollout("progressing", ["created", "step"]),
            rollout("succeeded", ["created", "step", "analysis", "succeeded"], "all steps healthy"),
        ]
    )
    assert run(api) == EXIT_SUCCEEDED
    assert read_outputs(outputs) == {
        "rollout_id": "7",
        "state": "succeeded",
        "reason": "all steps healthy",
    }
    out = capsys.readouterr().out
    assert sum("analysis" in line for line in out.splitlines()) == 1  # printed once


def test_rolled_back_exits_1(outputs):
    api = FakeApi([rollout("rolled_back", reason="error_rate=0.3 exceeds max 0.05")])
    assert run(api) == EXIT_FAILED
    assert read_outputs(outputs)["state"] == "rolled_back"


def test_promoting_is_still_in_progress():
    api = FakeApi([rollout("promoting"), rollout("succeeded", reason="ok")])
    assert run(api) == EXIT_SUCCEEDED


def test_conflict_exits_2(outputs, capsys):
    api = FakeApi([], post_status=409)
    assert run(api) == EXIT_UNKNOWN
    assert "could not start rollout (409)" in capsys.readouterr().out


def test_timeout_exits_2_and_leaves_rollout_running(outputs):
    api = FakeApi([rollout("progressing")] * 100)
    assert run(api, ARGS + ["--timeout", "10"]) == EXIT_UNKNOWN
    assert read_outputs(outputs)["reason"] == "timed out waiting"
    assert not any(r.url.path.endswith("/abort") for r in api.requests)


def test_timeout_can_abort():
    api = FakeApi([rollout("progressing")] * 100)
    run(api, ARGS + ["--timeout", "10", "--abort-on-timeout"])
    abort = [r for r in api.requests if r.url.path.endswith("/abort")]
    assert abort and json.loads(abort[0].content) == {"reason": "CI timeout"}


def test_transient_errors_are_retried():
    api = FakeApi([httpx.ConnectError("blip"), 502, rollout("succeeded", reason="ok")])
    assert run(api) == EXIT_SUCCEEDED


def test_controller_gone_exits_2(outputs):
    api = FakeApi([httpx.ConnectError("down")] * 10)
    assert run(api) == EXIT_UNKNOWN
    assert read_outputs(outputs)["state"] == "unknown"


def test_sends_token_and_policy(tmp_path, monkeypatch):
    monkeypatch.setenv("CANARY_API_TOKEN", "s3cret")
    policy = tmp_path / "p.yaml"
    policy.write_text("steps: [{weight: 100, pause_seconds: 1}]\n")
    api = FakeApi([rollout("succeeded", reason="ok")])
    run(api, ARGS + ["--policy", str(policy)])
    post = api.requests[0]
    assert post.headers["Authorization"] == "Bearer s3cret"
    assert json.loads(post.content)["policy"] == {"steps": [{"weight": 100, "pause_seconds": 1}]}
