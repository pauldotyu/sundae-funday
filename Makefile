UV ?= uv

.PHONY: sync lint format-check typecheck test check compose-config kustomize-local kustomize-azure build app-up demo-up down

sync:
	$(UV) sync --dev

lint:
	$(UV) run ruff check .

format-check:
	$(UV) run ruff format --check .

typecheck:
	$(UV) run pyright

test:
	$(UV) run pytest

check: lint format-check typecheck test

compose-config:
	docker compose config >/dev/null

kustomize-local:
	kubectl kustomize deploy/k8s/overlays/local >/dev/null

kustomize-azure:
	kubectl kustomize deploy/k8s/overlays/azure >/dev/null

build:
	docker compose build sundae-mcp ops-agent concierge

app-up:
	docker compose --profile app up -d --build

demo-up:
	docker compose --profile demo up -d --build --wait

down:
	docker compose --profile demo down
