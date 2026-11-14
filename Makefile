VENV := .venv
BIN := $(VENV)/bin
CLUSTER := canary
API := http://127.0.0.1:8020
TEST_DB := mysql+pymysql://canary:canary@127.0.0.1:3307/canary_test

.PHONY: help up venv db-up db-down test test-mysql lint fmt run \
        cluster cluster-down images deploy prometheus-forward demo-run \
        rollout-good rollout-bad rollout-slow status db-reset migrate migration \
        gateway-install gateway-deploy demo-run-gateway rollout-gateway-good rollout-gateway-bad

help:  ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

# --- local dev (no cluster needed) ----------------------------------------
up: venv db-up migrate  ## Create venv, start MySQL, apply migrations

venv:  ## Create .venv and install the package + dev tools
	test -d $(VENV) || python3 -m venv $(VENV)
	$(BIN)/pip install -q -e '.[dev]'

db-up:  ## Start MySQL on :3307 and wait until healthy
	docker compose up -d --wait mysql

db-down:  ## Stop MySQL (keeps data)
	docker compose down

db-reset:  ## Wipe the dev database and rebuild it from migrations
	docker compose down -v && docker compose up -d --wait mysql
	$(MAKE) migrate

migrate:  ## Apply all database migrations
	$(BIN)/alembic upgrade head

migration:  ## Generate a migration from model changes: make migration m="add foo"
	@test -n "$(m)" || (echo 'usage: make migration m="what changed"' && exit 1)
	$(BIN)/alembic revision --autogenerate -m "$(m)"
	@echo "Review the generated file before committing: autogenerate is a draft, not a decision."

test:  ## Run tests against in-memory SQLite
	$(BIN)/pytest -q

test-mysql: db-up  ## Run tests against real MySQL
	CANARY_TEST_DATABASE_URL=$(TEST_DB) $(BIN)/pytest -q

lint:  ## Lint + format check
	$(BIN)/ruff check . && $(BIN)/ruff format --check .

fmt:  ## Auto-format
	$(BIN)/ruff check --fix . && $(BIN)/ruff format .

run: migrate  ## Run the API on :8020 with the log-only router (no cluster)
	CANARY_ROUTER=log $(BIN)/uvicorn --factory canary.api:create_app --port 8020 --reload

# --- full demo on kind ----------------------------------------------------
cluster:  ## Create the kind cluster
	kind create cluster --config deploy/kind.yaml

cluster-down:  ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER)

images:  ## Build demo images (v1, v2 good, v2-bad 30% errors, v2-slow +150ms) and load into kind
	docker build -t demo-app:v1 --build-arg VERSION=v1 demo-app
	docker build -t demo-app:v2 --build-arg VERSION=v2 demo-app
	docker build -t demo-app:v2-bad --build-arg VERSION=v2-bad --build-arg ERROR_RATE=0.3 demo-app
	docker build -t demo-app:v2-slow --build-arg VERSION=v2-slow --build-arg EXTRA_LATENCY_MS=150 demo-app
	kind load docker-image demo-app:v1 demo-app:v2 demo-app:v2-bad demo-app:v2-slow --name $(CLUSTER)

deploy:  ## Apply demo app, load generator and Prometheus
	kubectl apply -f deploy/k8s/
	kubectl rollout status deploy/demo-stable deploy/prometheus --timeout=120s

prometheus-forward:  ## Expose in-cluster Prometheus on :9095 (leave running)
	kubectl port-forward svc/prometheus 9095:9090

demo-run: migrate  ## Run the controller against the kind cluster (pod-ratio routing)
	CANARY_ROUTER=replicas $(BIN)/uvicorn --factory canary.api:create_app --port 8020

rollout-good:  ## Start a rollout that should succeed, and watch it (~3 min)
	$(BIN)/python scripts/watch_rollout.py demo-app:v2 demo $(API)

rollout-bad:  ## Start a rollout that should auto-rollback, and watch it (~1 min)
	$(BIN)/python scripts/watch_rollout.py demo-app:v2-bad demo $(API)

rollout-slow:  ## Rollout under the 500ms ceiling but 150ms slower than stable: rolled back
	$(BIN)/python scripts/watch_rollout.py demo-app:v2-slow demo $(API)

# --- weighted routing with Gateway API (Week 5) -------------------------------
ENVOY_GATEWAY_VERSION := v1.9.3
GATEWAY_POLICY := policies/gateway.yaml

gateway-install:  ## Install Envoy Gateway (CRDs + controller) into the kind cluster
	kubectl apply --server-side -f https://github.com/envoyproxy/gateway/releases/download/$(ENVOY_GATEWAY_VERSION)/install.yaml
	kubectl wait --timeout=5m -n envoy-gateway-system deployment/envoy-gateway --for=condition=Available

gateway-deploy:  ## Create the Gateway + HTTPRoute and send load through Envoy
	kubectl apply -f deploy/gateway/
	kubectl wait --timeout=3m gateway/demo-gw --for=condition=Programmed
	kubectl set env deploy/loadgen TARGET=http://demo-gateway.envoy-gateway-system/
	kubectl rollout status deploy/loadgen --timeout=60s

demo-run-gateway: migrate  ## Run the controller with weighted routing
	CANARY_ROUTER=gateway $(BIN)/uvicorn --factory canary.api:create_app --port 8020

rollout-gateway-good:  ## 5% -> 25% -> 50% -> 100%, promoted (~5 min)
	$(BIN)/python scripts/watch_rollout.py demo-app:v2 demo $(API) $(GATEWAY_POLICY)

rollout-gateway-bad:  ## Bad release caught at 5% of traffic, not 25%
	$(BIN)/python scripts/watch_rollout.py demo-app:v2-bad demo $(API) $(GATEWAY_POLICY)

status:  ## Show recent rollouts and pod split
	curl -s '$(API)/rollouts?limit=5' | python3 -m json.tool
	-kubectl get deploy demo-stable demo-canary
