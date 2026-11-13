VENV := .venv
BIN := $(VENV)/bin
CLUSTER := canary
API := http://127.0.0.1:8020
TEST_DB := mysql+pymysql://canary:canary@127.0.0.1:3307/canary_test

.PHONY: help up venv db-up db-down test test-mysql lint fmt run \
        cluster cluster-down images deploy prometheus-forward demo-run \
        rollout-good rollout-bad status

help:  ## List targets
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

# --- local dev (no cluster needed) ----------------------------------------
up: venv db-up  ## Create venv and start MySQL

venv:  ## Create .venv and install the package + dev tools
	test -d $(VENV) || python3 -m venv $(VENV)
	$(BIN)/pip install -q -e '.[dev]'

db-up:  ## Start MySQL on :3307 and wait until healthy
	docker compose up -d --wait mysql

db-down:  ## Stop MySQL (keeps data)
	docker compose down

test:  ## Run tests against in-memory SQLite
	$(BIN)/pytest -q

test-mysql: db-up  ## Run tests against real MySQL
	CANARY_TEST_DATABASE_URL=$(TEST_DB) $(BIN)/pytest -q

lint:  ## Lint + format check
	$(BIN)/ruff check . && $(BIN)/ruff format --check .

fmt:  ## Auto-format
	$(BIN)/ruff check --fix . && $(BIN)/ruff format .

run:  ## Run the API on :8020 with the log-only router (no cluster)
	CANARY_ROUTER=log $(BIN)/uvicorn --factory canary.api:create_app --port 8020 --reload

# --- full demo on kind ----------------------------------------------------
cluster:  ## Create the kind cluster
	kind create cluster --config deploy/kind.yaml

cluster-down:  ## Delete the kind cluster
	kind delete cluster --name $(CLUSTER)

images:  ## Build demo images (v1, v2 good, v2-bad 30% errors) and load into kind
	docker build -t demo-app:v1 --build-arg VERSION=v1 demo-app
	docker build -t demo-app:v2 --build-arg VERSION=v2 demo-app
	docker build -t demo-app:v2-bad --build-arg VERSION=v2-bad --build-arg ERROR_RATE=0.3 demo-app
	kind load docker-image demo-app:v1 demo-app:v2 demo-app:v2-bad --name $(CLUSTER)

deploy:  ## Apply demo app, load generator and Prometheus
	kubectl apply -f deploy/k8s/
	kubectl rollout status deploy/demo-stable deploy/prometheus --timeout=120s

prometheus-forward:  ## Expose in-cluster Prometheus on :9095 (leave running)
	kubectl port-forward svc/prometheus 9095:9090

demo-run:  ## Run the controller against the kind cluster
	CANARY_ROUTER=kubernetes $(BIN)/uvicorn --factory canary.api:create_app --port 8020

rollout-good:  ## Start a rollout that should succeed
	curl -s -XPOST $(API)/rollouts -H 'content-type: application/json' \
	  -d '{"app":"demo","image":"demo-app:v2"}' | python3 -m json.tool

rollout-bad:  ## Start a rollout that should auto-rollback
	curl -s -XPOST $(API)/rollouts -H 'content-type: application/json' \
	  -d '{"app":"demo","image":"demo-app:v2-bad"}' | python3 -m json.tool

status:  ## Show recent rollouts and pod split
	curl -s '$(API)/rollouts?limit=5' | python3 -m json.tool
	-kubectl get deploy demo-stable demo-canary
