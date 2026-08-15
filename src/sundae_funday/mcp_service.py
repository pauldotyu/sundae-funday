"""Sundae MCP service."""

import contextlib
from functools import lru_cache
from typing import Any

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from sundae_funday.settings import AppSettings
from sundae_funday.shop import InMemorySundaeShop
from sundae_funday.telemetry import configure, create_metrics_app, instrument_asgi


class Settings(AppSettings):
    pass


@lru_cache
def get_settings() -> Settings:
    return Settings()


shop = InMemorySundaeShop()
mcp = FastMCP(name="SundaeTools", stateless_http=True)
mcp.settings.streamable_http_path = "/"
mcp.settings.transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=False
)


@mcp.tool()
def list_menu() -> dict[str, Any]:
    """Return the deterministic sundae menu and prices in integer cents."""
    return shop.list_menu()


@mcp.tool()
def check_availability(
    flavors: list[str] | None = None,
    sauce: str | None = None,
    toppings: list[str] | None = None,
) -> dict[str, Any]:
    """Return current inventory and availability for requested menu parts."""
    return shop.check_availability(
        flavors=flavors,
        sauce=sauce,
        toppings=toppings,
    )


@mcp.tool()
def quote_order(
    session_id: str,
    size: str = "CLASSIC",
    flavors: list[str] | None = None,
    sauce: str | None = None,
    toppings: list[str] | None = None,
    requested_ready_in_minutes: int | None = None,
) -> dict[str, Any]:
    """Create a priced draft for a sundae order without submitting it."""
    return shop.quote_order(
        session_id=session_id,
        size=size,
        flavors=flavors,
        sauce=sauce,
        toppings=toppings,
        requested_ready_in_minutes=requested_ready_in_minutes,
    )


@mcp.tool()
def submit_order(
    draft_id: str,
    session_id: str,
    idempotency_key: str,
    customer_name: str = "Guest",
) -> dict[str, Any]:
    """Submit a confirmed draft exactly once for an idempotency key."""
    return shop.submit_order(
        draft_id=draft_id,
        session_id=session_id,
        idempotency_key=idempotency_key,
        customer_name=customer_name,
    )


def create_app() -> Any:
    settings = get_settings()
    configure("sundae-mcp")

    async def health(_: Any) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": settings.app_version,
                "tools": 4,
            }
        )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(mcp.session_manager.run())
            yield

    app = Starlette(
        routes=[
            Route("/healthz", health),
            Mount("/metrics", create_metrics_app()),
            Mount("/mcp", mcp.streamable_http_app()),
        ],
        lifespan=lifespan,
    )
    return instrument_asgi(app)
