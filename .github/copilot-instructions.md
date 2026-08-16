# Copilot instructions

## Build, test, and lint

- Use Python 3.12 and `uv`. Install the locked development environment with `make sync` (`uv sync --frozen --dev`).
- Run the full local validation suite with `make check`. It runs, in order: `ruff check .`, `ruff format --check .`, `pyright`, and `pytest`.
- Apply formatting explicitly with `make format`; `make check` never modifies files.
- Run all tests with `make test` or `uv run pytest`.
- Run one test file with `uv run pytest tests/test_shop.py`.
- Run one test with `uv run pytest tests/test_shop.py::test_quote_creates_ready_draft_with_integer_cents`.
- Build the shared application image with `make build`. Start or stop the full local stack with `make up` and `make down`.
- Kubernetes changes must also pass: `helm lint deploy/helm/sundae-funday` and `helm template sundae-funday deploy/helm/sundae-funday --namespace demo`. Render the local and Azure profiles too when changing chart configuration.

## Architecture

- One Python package and one container image implement three ASGI services. `sundae-funday-serve` reads `SERVICE` and selects the app factory and default port in `run_service.py`: `sundae-mcp` (8101), `ops-agent` (8202), or `concierge` (8301).
- `mcp_service.py` is the authoritative deterministic backend. Its FastMCP tools delegate to the process-local `InMemorySundaeShop`, which owns catalog resolution, inventory, drafts, idempotency, pricing, and submission.
- `ops_agent.py` exposes an A2A operations specialist. It must ground operational answers in Sundae MCP tools and must never submit orders. Structured internal requests use the `SUNDAE_OPS_REQUEST ` prefix to bypass model ambiguity for inventory specials and fulfillment verification.
- The concierge is split by responsibility: `concierge/app.py` exposes HTTP, `routing.py` performs deterministic extraction and plan merging, `runtime.py` orchestrates MCP/A2A/model calls and session state, `state.py` stores conversation and pending drafts, and `presentation.py` renders deterministic fallback responses.
- Concierge routing and writing may use a model, but the application remains usable without one. Empty model settings disable both agents; invalid or empty model output retries once and then falls back to heuristic routing or deterministic rendering.
- The order lifecycle is intentionally two phase: `quote_order` creates a draft, Scooper verifies fulfillment, and only `/api/confirm` calls `submit_order`. Quoting must not decrement inventory.
- Compose and Helm deploy the same image three times with different `SERVICE` and `PORT` values. Compose also runs the local observability stack. The local Helm profile uses host networking and loopback service URLs; the default and Azure profiles use normal Kubernetes networking.
- `telemetry.py` owns the shared OpenTelemetry setup, and every service exposes `/healthz` and `/metrics`. Preserve the tracing behavior described below when changing service boundaries or clients.

## Agent tracing and observability

- This repository is primarily an agent-tracing sample. Preserve one distributed trace across the important paths: browser request -> concierge -> Sundae MCP, and browser request -> concierge -> Ops A2A agent -> Sundae MCP.
- Call `configure()` with the stable service names `concierge`, `ops-agent`, and `sundae-mcp` before creating instrumented traffic. Each deployed process runs exactly one selected service, so `telemetry.py` intentionally configures the global OpenTelemetry providers only once.
- Return `instrument_asgi(app)` from every app factory. ASGI instrumentation intentionally excludes `/healthz`, `/metrics`, and low-value receive/send spans; do not add probe or metrics noise back into traces.
- Keep the manual business spans and their semantic attributes: `concierge.chat` records `conversation.id`, `gen_ai.operation.name`, `gen_ai.agent.name`, message length, selected route, and confirmation state; `concierge.confirm` records `conversation.id`; `mcp.client.call_tool` records `rpc.system`, `rpc.method`, and `gen_ai.tool.name`.
- Context propagation is explicit at every outbound agent boundary. MCP calls create an `httpx.AsyncClient` with `inject_trace_headers()`. A2A calls install an HTTPX request hook that injects the current headers. Ops MCP tooling uses `header_provider=inject_trace_headers`. Any new HTTP, MCP, or A2A client must propagate the active context the same way.
- The dedicated HTTPX client used by `MCPStreamableHTTPTool` is deliberately removed from global HTTPX auto-instrumentation to avoid duplicate client spans; trace headers are still injected by its header provider. Do not remove one mechanism without checking the resulting parent/child trace shape.
- `ENABLE_INSTRUMENTATION=false` must disable provider setup, HTTPX instrumentation, and ASGI middleware together. App-factory tests set this to avoid global provider state leaking between tests.
- `APPLICATIONINSIGHTS_CONNECTION_STRING` selects Azure Monitor exporters for traces, metrics, and logs. When it is empty, the standard OTLP HTTP exporters use `OTEL_EXPORTER_OTLP_ENDPOINT`. Treat this as one exporter choice; setting the connection string means the OTLP exporter path is not used.
- Local OTLP data flows through `otel-collector`: traces go to Tempo, metrics to its Prometheus exporter, and logs to Loki. Grafana datasources are provisioned for those backends. Keep collector ports and Compose endpoints synchronized.
- Preserve `OTEL_SERVICE_NAMESPACE` across all three services so traces from the shared image group together while `service.name` distinguishes each hop. Compose, default Helm values, and local/Azure profile values are the deployment sources for this telemetry configuration.
- When changing protocol clients or tracing code, keep `tests/test_clients.py` coverage for current `traceparent` injection and add focused assertions for any new propagation boundary or stable span attribute.

## Repository conventions

- Keep catalog identifiers as uppercase SKUs and user-facing names separate. Normalize external input through `catalog.normalize_token` and `ALIASES`; preserve catalog insertion order because response-shape tests depend on it.
- Monetary values are authoritative integer cents with a separate display string. Do not use floating-point values for pricing.
- Treat MCP and Ops results as authoritative data. Model-generated text may present those results but must not invent menu, price, stock, ETA, or order facts.
- Preserve explicit failure behavior at service boundaries: malformed, missing, or failed MCP/A2A results raise `RuntimeError`; the FastAPI layer converts concierge runtime errors to HTTP 400 responses.
- Settings extend `AppSettings`, come from environment variables and `.env`, and use Pydantic validators for required combinations. Normalize MCP base URLs with `normalize_url`.
- App modules expose `create_app()` factories rather than module-level ASGI app instances. Tests inject async MCP and Ops callables into `ConciergeRuntime` instead of starting dependent services.
- Session state is process-local. Preserve draft ownership, single submission, and idempotency-key replay semantics when changing shop or confirmation code.
- Keep deployment surfaces synchronized: service names, ports, health checks, image version, environment variables, and security settings are asserted in `tests/test_deployment_contracts.py`. The project version in `pyproject.toml` is the default image and chart app version, and release tags must match it.
