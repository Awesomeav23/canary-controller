"""canary-rollout: start a rollout and block until it finishes. Built for CI.

    canary-rollout --api https://canary.example.com --app demo --image demo-app:abc123

Exit codes: 0 succeeded, 1 rolled back or aborted, 2 couldn't tell (timeout, API error,
another rollout already active). In GitHub Actions it also writes rollout_id, state and
reason to $GITHUB_OUTPUT.
"""

import argparse
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import yaml

ACTIVE = {"pending", "progressing", "promoting"}
EXIT_SUCCEEDED, EXIT_FAILED, EXIT_UNKNOWN = 0, 1, 2


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="canary-rollout", description=__doc__.splitlines()[0])
    p.add_argument("--api", required=True, help="controller base URL")
    p.add_argument("--app", required=True)
    p.add_argument("--image", required=True)
    p.add_argument("--policy", type=Path, help="policy YAML (default: the controller's)")
    p.add_argument("--timeout", type=float, default=1800, help="seconds to wait (default 1800)")
    p.add_argument("--poll", type=float, default=5, help="seconds between polls (default 5)")
    p.add_argument(
        "--abort-on-timeout",
        action="store_true",
        help="abort the rollout if it hasn't finished in time (default: leave it running)",
    )
    return p.parse_args(argv)


def write_outputs(**values: object) -> None:
    path = os.getenv("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a") as f:
        for k, v in values.items():
            f.write(f"{k}={'' if v is None else str(v).replace(chr(10), ' ')}\n")


class Api:
    """Thin client with retries: one dropped connection shouldn't fail a deploy job."""

    def __init__(self, client: httpx.Client, sleep: Callable[[float], None], retries: int = 3):
        self.client, self.sleep, self.retries = client, sleep, retries

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(self.retries + 1):
            try:
                resp = self.client.request(method, url, **kwargs)
                if resp.status_code < 500:
                    return resp
                error: Exception = httpx.HTTPStatusError(
                    f"{resp.status_code}", request=resp.request, response=resp
                )
            except httpx.TransportError as e:
                error = e
            if attempt < self.retries:
                self.sleep(2**attempt)
        raise error


def main(
    argv: list[str] | None = None,
    transport: httpx.BaseTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> int:
    args = parse_args(argv)
    headers = {}
    if token := os.getenv("CANARY_API_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"
    client = httpx.Client(
        base_url=args.api.rstrip("/"), headers=headers, timeout=10, transport=transport
    )
    api = Api(client, sleep)

    body: dict = {"app": args.app, "image": args.image}
    if args.policy:
        body["policy"] = yaml.safe_load(args.policy.read_text())

    try:
        resp = api.request("POST", "/rollouts", json=body)
    except httpx.HTTPError as e:
        print(f"::error::could not reach the controller: {e}")
        return EXIT_UNKNOWN
    if resp.status_code != 201:
        print(f"::error::could not start rollout ({resp.status_code}): {resp.text}")
        return EXIT_UNKNOWN

    rollout = resp.json()
    rollout_id = rollout["id"]
    print(f"rollout {rollout_id}: {args.app} -> {args.image}")
    write_outputs(rollout_id=rollout_id)

    seen, deadline = 0, monotonic() + args.timeout
    while True:
        for e in rollout["events"][seen:]:
            print(f"  {e['at'][11:19]}  {e['kind']:<16} {e['message']}")
        seen = len(rollout["events"])

        if rollout["state"] not in ACTIVE:
            break
        if monotonic() >= deadline:
            print(
                f"::error::rollout {rollout_id} still {rollout['state']} after {args.timeout:.0f}s"
            )
            if args.abort_on_timeout:
                api.request("POST", f"/rollouts/{rollout_id}/abort", json={"reason": "CI timeout"})
                print("aborted it (--abort-on-timeout)")
            write_outputs(state=rollout["state"], reason="timed out waiting")
            return EXIT_UNKNOWN

        sleep(args.poll)
        try:
            rollout = api.request("GET", f"/rollouts/{rollout_id}").json()
        except httpx.HTTPError as e:
            print(f"::error::lost contact with the controller: {e}")
            write_outputs(state="unknown", reason=str(e))
            return EXIT_UNKNOWN

    state, reason = rollout["state"], rollout["reason"]
    write_outputs(state=state, reason=reason)
    if state == "succeeded":
        print(f"rollout {rollout_id} succeeded")
        return EXIT_SUCCEEDED
    print(f"::error::rollout {rollout_id} {state}: {reason}")
    return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
