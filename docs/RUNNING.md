# Running canary-controller locally

A step-by-step guide to running and trying the project on your machine. For what it does
and why, see the [README](../README.md).

There are three ways to run it. Each builds on the one before:

| | What you get | Needs | Time |
|---|---|---|---|
| **A. API only** | The API, database and tests. No real rollouts | Python, Docker | 2 min |
| **B. Cluster, pod-ratio routing** | Real rollouts on Kubernetes (25% steps) | + kind, kubectl | 10 min |
| **C. Cluster, weighted routing** | 5% steps through Envoy Gateway | + B | +5 min |

## Prerequisites

| Tool | Check | Install (macOS) |
|---|---|---|
| Python 3.11+ | `python3 --version` | `brew install python` |
| Docker (running) | `docker info` | Docker Desktop. Give it about 4 GB of memory |
| kind (B, C) | `kind version` | `brew install kind` |
| kubectl (B, C) | `kubectl version --client` | `brew install kubectl` |

Ports used on your machine:

| Port | What |
|---|---|
| 3307 | MySQL (Docker) |
| 8020 | The controller's API |
| 9095 | Prometheus (port-forward from the cluster) |
| 8090 | Optional local webhook receiver (for notifications) |

`make help` lists every command.

---

## A. API only (no cluster)

```bash
git clone https://github.com/Awesomeav23/canary-controller.git
cd canary-controller
make up          # .venv, MySQL on :3307, database migrations
make test        # 112 tests on SQLite, ~1 s; all should pass
make test-mysql  # the same tests on real MySQL
make run         # API on http://127.0.0.1:8020 (leave running)
```

Open **http://127.0.0.1:8020/docs**. Every endpoint can be tried from there
("Try it out" → "Execute").

In this mode the controller only logs what it would do to a cluster, and there is no
Prometheus. So any rollout you start will **roll back with `no data`**. That's the
fail-closed behaviour working. Use B for real rollouts.

---

## B. Full demo on kind (pod-ratio routing)

You need **3 terminals**, all in the repo folder.

**Terminal 1: build the cluster** (first time about 5 min, mostly image builds)

```bash
make up                       # skip if you did A
make cluster                  # local Kubernetes cluster "canary"
make images                   # builds 4 demo app versions, loads them into kind
make deploy                   # demo app (4 pods), load generator, Prometheus
```

Check: `kubectl get pods` shows 4 `demo-stable-…`, 1 `loadgen-…`, 1 `prometheus-…`, all
`Running`.

**Terminal 2: Prometheus** (leave running)

```bash
make prometheus-forward       # http://localhost:9095
```

**Terminal 3: the controller** (leave running)

```bash
make demo-run                 # applies migrations, then serves on :8020
```

**Back in terminal 1: roll out**

```bash
make rollout-bad     # ~1 min.  v2-bad (30% errors): rolled back at 25%
make rollout-slow    # ~1 min.  v2-slow (+150 ms): rolled back, slower than stable
make rollout-good    # ~3 min.  v2: 25% -> 50% -> 100%, promoted
make status          # recent rollouts + current pod split
```

Each line shows the event and the pod split, and the last line is `FINAL: <state>`:

```text
[   4s] step         step 0: canary at 25%                      | demo-stable=3 demo-canary=1
[  65s] analysis     step 0 (25%): error_rate=0.3074 exceeds max 0.05 | demo-stable=4 demo-canary=0
FINAL: rolled_back - error_rate=0.3074 exceeds max 0.05
```

---

## C. Weighted routing (Envoy Gateway)

Do B first. Then stop the controller in terminal 3 (Ctrl-C) and:

**Terminal 1**

```bash
make gateway-install   # Envoy Gateway v1.9.3 into the cluster (~1 min)
make gateway-deploy    # Gateway + route; the load generator now goes through Envoy
```

Check: `kubectl get gateway demo-gw` shows `PROGRAMMED  True`.

**Terminal 3**

```bash
make demo-run-gateway
```

**Terminal 1**

```bash
make rollout-gateway-bad    # ~1 min.  Caught at 5% of traffic
make rollout-gateway-good   # ~5 min.  5% -> 25% -> 50% -> 100%, promoted
```

The lines now also show the route weights, e.g. `route demo-stable:95 demo-canary:5`.
Stable keeps all 4 pods throughout.

### Switching back to pod-ratio mode

`gateway-deploy` points the load generator at Envoy. Pod-ratio mode needs it pointed back
at the Service, or the canary gets no traffic and every rollout rolls back with
`no data`:

```bash
kubectl set env deploy/loadgen TARGET=http://demo/
```

(`make deploy` also resets it, because it re-applies `deploy/k8s/loadgen.yaml`. So run
`make gateway-deploy` again if you then go back to gateway mode.)

---

## Turning on the API token

Without `CANARY_API_TOKEN`, anyone who can reach the API can start rollouts. To require a
token for writes:

```bash
# terminal 3 (controller)
CANARY_API_TOKEN=my-token make demo-run-gateway

# terminal 1: the rollout commands need it too, or they get 401
export CANARY_API_TOKEN=my-token
make rollout-gateway-bad
```

In the browser: on http://127.0.0.1:8020/docs click **Authorize** (top right), paste
`my-token`, then use the endpoints with a lock icon. Reads (`GET`) never need the
token.

---

## Notifications (Slack / webhooks) without Slack

A small local receiver stands in for Slack. It prints every message it gets, and can
fail its first N requests so you can watch retries.

```bash
# new terminal: receiver that fails its first 2 requests
.venv/bin/python scripts/webhook_receiver.py 8090 2

# terminal 3: controller with both kinds of notification pointed at it
CANARY_SLACK_WEBHOOK_URL=http://127.0.0.1:8090/slack \
CANARY_WEBHOOK_URLS=http://127.0.0.1:8090/hook \
make demo-run-gateway
```

Run a rollout. Within about 15 s the receiver prints the first two attempts as `503`
(simulated outage), then the retried deliveries:

```text
503 /slack (simulated outage, 1 more)
503 /hook (simulated outage, 0 more)
200 /slack delivery=- {"text": ":rocket: *demo* `demo-app:v2-bad` rollout started (rollout 7)"}
200 /hook delivery=2 {"delivery_id": 2, "event": "started", ...}
```

Delivery status per rollout: `curl localhost:8020/rollouts/<id>/notifications`.

To use real Slack, create an
[incoming webhook](https://api.slack.com/messaging/webhooks) and pass its URL as
`CANARY_SLACK_WEBHOOK_URL`.

---

## The CI tool

`canary-rollout` is what CI runs. It starts a rollout and waits:

```bash
.venv/bin/canary-rollout --api http://127.0.0.1:8020 --app demo \
  --image demo-app:v2-bad --policy policies/gateway.yaml
echo "exit code: $?"    # 0 succeeded, 1 rolled back/aborted, 2 timeout or error
```

It uses `CANARY_API_TOKEN` from the environment if set.

---

## Things to try

| Try | What should happen |
|---|---|
| Start a rollout while one is running for the same app | `409 demo already has an active rollout` |
| Write without the token (when one is set) | `401` |
| `PUT /freezes/demo` in /docs, then start a rollout | It stays `pending` (event `blocked`). `DELETE /freezes/demo` and it starts |
| Abort a running rollout (command below) | Rolled back immediately, state `aborted` |
| Restart the controller (Ctrl-C, run again) mid-rollout | It carries on from the same step |
| Watch traffic in Prometheus while a rollout runs | Canary line rises at each step (query below) |

Abort (use the id printed as `rollout N:`):

```bash
curl -X POST localhost:8020/rollouts/<id>/abort \
  -H "Authorization: Bearer $CANARY_API_TOKEN" \
  -H 'content-type: application/json' -d '{"reason": "trying abort"}'
```

Traffic graph: open http://localhost:9095/graph and run

```text
sum by (track) (rate(http_requests_total{app="demo"}[20s]))
```

The controller's own metrics: http://127.0.0.1:8020/metrics (look for `canary_`).

---

## Settings

All optional. Set them as environment variables on the controller.

| Variable | Default | What |
|---|---|---|
| `CANARY_ROUTER` | `log` | `log` (no cluster), `replicas` (B), `gateway` (C). The make targets set it |
| `CANARY_DATABASE_URL` | MySQL on `127.0.0.1:3307` | SQLAlchemy URL |
| `CANARY_PROMETHEUS_URL` | `http://127.0.0.1:9095` | Where to read canary metrics |
| `CANARY_NAMESPACE` | `default` | Kubernetes namespace of the app |
| `CANARY_TICK_SECONDS` | `5` | How often the controller advances rollouts |
| `CANARY_DEFAULT_POLICY` | `policies/default.yaml` | Policy used when a request has none |
| `CANARY_API_TOKEN` | (none) | Require `Authorization: Bearer <token>` for writes |
| `CANARY_SLACK_WEBHOOK_URL` | (none) | Slack incoming webhook |
| `CANARY_WEBHOOK_URLS` | (none) | Comma-separated JSON webhook URLs |

Rollout steps and thresholds live in `policies/*.yaml`. The README's
[Rollout policy](../README.md#rollout-policy) section explains each field.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Controller won't start: `database schema is at X, code expects Y` | New migrations since you last ran | `make migrate` |
| `make rollout-*` prints `401` | Controller has a token, your terminal doesn't | `export CANARY_API_TOKEN=…` in that terminal |
| `409 … already has an active rollout` | One is still running | Wait, or abort it (see above) |
| Every rollout rolls back with `no data` | Prometheus port-forward stopped, or the load generator points at the wrong place for this mode | Check `curl localhost:9095/-/ready` (rerun `make prometheus-forward`). Check the load generator target: [Switching back](#switching-back-to-pod-ratio-mode) |
| `canary pods not ready after 180s` | Demo images not loaded into kind (e.g. after recreating the cluster) | `make images` |
| `make gateway-deploy` hangs on `Programmed` | Envoy Gateway not installed or still starting | `make gateway-install`, then retry |
| `Bind for 0.0.0.0:3307 failed` | Something else uses port 3307 | Change the port in `docker-compose.yml` and set `CANARY_DATABASE_URL` to match |
| kubectl talks to the wrong cluster | Context isn't kind's | `kubectl config use-context kind-canary` |

---

## Cleaning up

```bash
# Ctrl-C the controller, the port-forward and the receiver, then:
make cluster-down    # deletes the kind cluster (and Envoy Gateway in it)
make db-down         # stops MySQL, keeps its data
docker compose down -v   # stops MySQL and deletes its data
```
