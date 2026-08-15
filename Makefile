UV ?= uv
PROJECT_VERSION := $(shell python3 -c 'import tomllib; print(tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')
IMAGE_TAG ?= $(PROJECT_VERSION)

# -- Development -------------------------------------------------------------------

.PHONY: sync lint format-check typecheck test check build up down \
	azure-deploy \
	kind-create kind-delete

sync:
	$(UV) sync --frozen --dev

lint:
	$(UV) run ruff check .

format:
	$(UV) run ruff format .

format-check:
	$(UV) run ruff format --check .

typecheck:
	$(UV) run pyright

test:
	$(UV) run pytest

check: lint format-check typecheck test

# -- Build -------------------------------------------------------------------------

build:
	IMAGE_TAG=$(IMAGE_TAG) docker compose build sundae-mcp

# -- Local (Docker Compose + Ollama) ----------------------------------------------

up:
	IMAGE_TAG=$(IMAGE_TAG) docker compose up -d --build --wait

down:
	docker compose down

# -- Azure -------------------------------------------------------------------------

azure-deploy:
	$(UV) run python scripts/azure_deploy.py

# -- Kubernetes on Kind ------------------------------------------------------------

IMAGE := sundae-funday:$(IMAGE_TAG)
OPENAI_BASE_URL ?= http://172.17.0.1:11434/v1
OTEL_EXPORTER_OTLP_ENDPOINT ?= http://172.17.0.1:4318

kind-create: build
	docker compose up -d otel-collector loki tempo prometheus grafana
	kind create cluster --name sundae --config deploy/kind-config.yaml 2>&1 || true
	kubectl config use-context kind-sundae
	kind load docker-image $(IMAGE) --name sundae
	helm upgrade --install sundae-funday deploy/helm/sundae-funday \
		--namespace demo --create-namespace \
		--values deploy/helm/values-local.yaml \
		--set-string image.tag=$(IMAGE_TAG) \
		--set-string config.OPENAI_BASE_URL=$(OPENAI_BASE_URL) \
		--set-string config.OTEL_EXPORTER_OTLP_ENDPOINT=$(OTEL_EXPORTER_OTLP_ENDPOINT) \
		--wait

kind-delete:
	docker compose down otel-collector loki tempo prometheus grafana
	helm uninstall sundae-funday --namespace demo --ignore-not-found 2>/dev/null || true
	kind delete cluster --name sundae 2>/dev/null || true
