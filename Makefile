UV ?= uv

# -- Development -------------------------------------------------------------------

.PHONY: sync lint format-check typecheck test check build up down \
	azure-deploy \
	kind-create kind-delete

sync:
	$(UV) sync --dev

lint:
	$(UV) run ruff check .

format :
	$(UV) run ruff format .

format-check:
	$(UV) run ruff format --check .

typecheck:
	$(UV) run pyright

test:
	$(UV) run pytest

check: lint format format-check typecheck test

# -- Build -------------------------------------------------------------------------

build:
	docker compose build sundae-mcp ops-agent concierge

# -- Local (Docker Compose + Ollama) ----------------------------------------------

up:
	docker compose --profile demo up -d --build --wait

down:
	docker compose --profile demo down

# -- Azure -------------------------------------------------------------------------

azure-deploy:
	bash scripts/azure-deploy.sh

# -- Kubernetes on Kind ------------------------------------------------------------

IMAGE_TAGS := sundae-funday/sundae-mcp:0.1.0 \
              sundae-funday/ops-agent:0.1.0 \
              sundae-funday/concierge:0.1.0

kind-create: build
	kind create cluster --name sundae --config deploy/k8s/kind-config.yaml 2>&1 || true
	kubectl config use-context kind-sundae
	kind load docker-image $(IMAGE_TAGS) --name sundae
	kubectl apply -k deploy/k8s/overlays/local
	kubectl apply -k deploy/k8s/overlays/local

kind-delete:
	kubectl delete -k deploy/k8s/overlays/local --ignore-not-found 2>/dev/null || true
	kubectl delete -k deploy/k8s/overlays/local --ignore-not-found 2>/dev/null || true
	kind delete cluster --name sundae 2>/dev/null || true
