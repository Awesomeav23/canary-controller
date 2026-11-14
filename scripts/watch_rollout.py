"""Start a rollout and print each event live, with the stable/canary replica split.

Usage: python scripts/watch_rollout.py <image> [app] [api_url] [policy.yaml]
"""

import subprocess
import sys
import time

import httpx
import yaml

ACTIVE = {"pending", "progressing", "promoting"}


def replica_split(app: str) -> str:
    out = subprocess.run(
        [
            "kubectl",
            "get",
            "deploy",
            f"{app}-stable",
            f"{app}-canary",
            "-o",
            "jsonpath={range .items[*]}{.metadata.name}={.spec.replicas} {end}",
        ],
        capture_output=True,
        text=True,
    )
    split = out.stdout.strip() or "(no cluster)"
    route = subprocess.run(
        [
            "kubectl",
            "get",
            "httproute",
            app,
            "-o",
            "jsonpath={range .spec.rules[0].backendRefs[*]}{.name}:{.weight} {end}",
        ],
        capture_output=True,
        text=True,
    ).stdout.strip()
    return f"{split} | route {route}" if route else split


def main() -> int:
    image = sys.argv[1]
    app = sys.argv[2] if len(sys.argv) > 2 else "demo"
    api = sys.argv[3] if len(sys.argv) > 3 else "http://127.0.0.1:8020"
    body: dict = {"app": app, "image": image}
    if len(sys.argv) > 4:
        with open(sys.argv[4]) as f:
            body["policy"] = yaml.safe_load(f)

    resp = httpx.post(f"{api}/rollouts", json=body)
    if resp.status_code != 201:
        print(f"could not start rollout: {resp.status_code} {resp.text}")
        return 1
    rollout_id = resp.json()["id"]
    print(f"rollout {rollout_id}: {app} -> {image}")

    seen, start = 0, time.time()
    while True:
        r = httpx.get(f"{api}/rollouts/{rollout_id}").json()
        for e in r["events"][seen:]:
            elapsed = time.time() - start
            print(f"[{elapsed:4.0f}s] {e['kind']:12} {e['message']:55} | {replica_split(app)}")
        seen = len(r["events"])
        if r["state"] not in ACTIVE:
            print(f"FINAL: {r['state']} - {r['reason']}")
            return 0  # a rollback is a valid outcome here, not a script failure
        time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
