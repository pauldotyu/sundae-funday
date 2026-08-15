"""A2A operations specialist grounded in Sundae MCP tools."""

import asyncio
import contextlib
import json
import logging
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any, Protocol, Self

import httpx
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Part,
    TaskState,
)
from agent_framework import (
    Agent,
    AgentResponse,
    AgentSession,
    FunctionInvocationContext,
    MCPStreamableHTTPTool,
    MiddlewareTermination,
    function_middleware,
)
from agent_framework.a2a import A2AExecutor
from agent_framework.exceptions import ToolException
from mcp.types import CallToolResult
from pydantic import field_validator, model_validator
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from sundae_funday.agent_runtime import run_agent_attempts
from sundae_funday.model_client import (
    OpenAIAuthMode,
    create_openai_chat_client,
    validate_openai_auth,
)
from sundae_funday.protocol import extract_function_results, result_text
from sundae_funday.settings import AppSettings, normalize_url
from sundae_funday.telemetry import (
    configure,
    create_metrics_app,
    inject_trace_headers,
    instrument_asgi,
    uninstrument_httpx_client,
)

logger = logging.getLogger("ops-agent")


class ConnectableMCPTool(Protocol):
    async def connect(self, *, reset: bool = False) -> None: ...


OPS_INSTRUCTIONS = """
You are Ops Scoop, the Sundae Funday operations specialist.
Call a Sundae MCP tool before every answer. Use the narrowest tool that fits.
Use list_menu for sizes, prices, flavors, sauces, and toppings.
Use check_availability for stock, shortages, and what can be made now.
For fulfillment verification, call check_availability with the exact requested
flavors, sauce, and toppings. Do not use quote_order for a fulfillment check.
For specials, call check_availability without filters so the concierge can use
the highest-inventory flavor, sauce, and toppings.
Use quote_order for hypothetical builds, pricing, and ETA questions. When you
need quote_order, pass session_id="ops-agent" and include requested_ready_in_minutes
when the customer gave a time target.
Never call submit_order. Only the concierge confirm action can submit an order.
Never invent stock, price, ETA, or menu facts.

If the user asks about ice cream prep time, flavor explanations, toppings, or
other general customer service topics, reply directly with helpful information
rather than calling a tool. You are not needed for those, they belong to the
concierge in the current conversation. Do not fail or raise errors for such
queries; just answer naturally.
""".strip()


class Settings(AppSettings):
    ops_agent_public_base_url: str = "http://ops-agent:8202"
    sundae_mcp_url: str = "http://sundae-mcp:8101/mcp/"
    openai_base_url: str = ""
    openai_chat_model: str = ""
    openai_auth_mode: OpenAIAuthMode = OpenAIAuthMode.API_KEY
    openai_api_key: str | None = None
    mcp_startup_attempts: int = 27
    mcp_startup_backoff_seconds: float = 1.0

    @field_validator("openai_base_url", "openai_chat_model")
    @classmethod
    def require_model_setting(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("model configuration values must not be empty")
        return value

    @model_validator(mode="after")
    def require_model_auth(self) -> Self:
        validate_openai_auth(self.openai_auth_mode, self.openai_api_key)
        return self

    @property
    def normalized_sundae_mcp_url(self) -> str:
        return normalize_url(self.sundae_mcp_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def parse_sundae_result(result: CallToolResult) -> str:
    return result_text(result, sort_keys=True)


@function_middleware
async def return_sundae_result(
    context: FunctionInvocationContext,
    call_next: Callable[[], Awaitable[None]],
) -> None:
    await call_next()
    raise MiddlewareTermination(result=context.result)


async def run_with_required_tool(
    agent: Agent,
    query: Any,
    session: AgentSession,
) -> list[str]:
    prompts = [
        str(query),
        (
            f"Original request:\n{query}\n\n"
            "Your previous response did not call a tool. Call exactly one "
            "Sundae MCP tool now and do not answer with text."
        ),
    ]

    async def execute(prompt: str) -> AgentResponse:
        return await agent.run(prompt, session=session, stream=False)

    def extract(response: AgentResponse) -> list[str] | None:
        results = extract_function_results(response)
        return results or None

    return await run_agent_attempts(prompts, execute, extract) or []


class SundaeOpsExecutor(A2AExecutor):
    def __init__(
        self,
        agent: Agent,
        sundae_tools: MCPStreamableHTTPTool,
    ) -> None:
        super().__init__(agent)
        self._ops_agent = agent
        self._sundae_tools = sundae_tools
        self._operations: dict[
            str,
            Callable[[dict[str, Any]], Awaitable[list[str]]],
        ] = {
            "inventory_special": self._inventory_special,
            "verify_fulfillment": self._verify_fulfillment,
        }

    async def _tool_text(
        self,
        name: str,
        **arguments: Any,
    ) -> list[str]:
        result = await self._sundae_tools.call_tool(name, **arguments)
        if isinstance(result, str):
            return [result]
        return [
            content.text
            for content in result
            if content.type == "text" and isinstance(content.text, str)
        ]

    async def _inventory_special(self, _: dict[str, Any]) -> list[str]:
        return await self._tool_text("check_availability")

    async def _verify_fulfillment(
        self,
        arguments: dict[str, Any],
    ) -> list[str]:
        return await self._tool_text(
            "check_availability",
            flavors=arguments.get("flavors"),
            sauce=arguments.get("sauce"),
            toppings=arguments.get("toppings"),
        )

    async def _run_structured_request(self, query: Any) -> list[str] | None:
        if not isinstance(query, str) or not query.startswith("SUNDAE_OPS_REQUEST "):
            return None
        try:
            request = json.loads(query.removeprefix("SUNDAE_OPS_REQUEST "))
        except json.JSONDecodeError as error:
            raise RuntimeError("Invalid structured Ops Scoop request") from error
        if not isinstance(request, dict):
            raise RuntimeError("Invalid structured Ops Scoop request")
        operation = request.get("operation")
        arguments = request.get("arguments", {})
        if not isinstance(arguments, dict):
            raise RuntimeError("Invalid structured Ops Scoop arguments")
        handler = self._operations.get(str(operation))
        if handler is None:
            raise RuntimeError(f"Unsupported Ops Scoop operation: {operation}")
        return await handler(arguments)

    async def _run(
        self,
        query: Any,
        session: AgentSession,
        updater: TaskUpdater,
    ) -> None:
        results = await self._run_structured_request(query)
        if results is None:
            results = await run_with_required_tool(self._ops_agent, query, session)
        if not results:
            raise RuntimeError("Ops Scoop completed without a Sundae MCP result")
        await updater.update_status(
            state=TaskState.TASK_STATE_WORKING,
            message=updater.new_agent_message(parts=[Part(text="\n".join(results))]),
        )


def create_agent_card(settings: Settings | None = None) -> AgentCard:
    settings = settings or get_settings()
    skill = AgentSkill(
        id="support_sundae_operations",
        name="Support Sundae Operations",
        description=(
            "Makes tool grounded recommendations about inventory, availability, "
            "pricing, and prep timing."
        ),
        tags=["sundae", "operations", "inventory", "eta", "pricing"],
        examples=[
            "What toppings are running low tonight?",
            "Can you make a deluxe mint chip sundae in ten minutes?",
            "Which sundae build is easiest to make right now?",
        ],
    )
    return AgentCard(
        name="Ops Scoop",
        description="Model driven operations specialist grounded in Sundae MCP.",
        version=settings.app_version,
        default_input_modes=["text"],
        default_output_modes=["text"],
        capabilities=AgentCapabilities(streaming=True),
        supported_interfaces=[
            AgentInterface(
                url=normalize_url(settings.ops_agent_public_base_url),
                protocol_binding="JSONRPC",
            )
        ],
        skills=[skill],
    )


def create_ops_agent(
    settings: Settings,
    sundae_tools: MCPStreamableHTTPTool,
    client: Any | None = None,
) -> Agent:
    model_client = client or create_openai_chat_client(
        model=settings.openai_chat_model,
        base_url=settings.openai_base_url,
        auth_mode=settings.openai_auth_mode,
        api_key=settings.openai_api_key,
        middleware=[return_sundae_result],
    )
    return Agent(
        client=model_client,
        name="OpsScoop",
        description="Sundae operations specialist",
        instructions=OPS_INSTRUCTIONS,
        tools=sundae_tools,
        default_options={
            "temperature": 0.1,
            "max_tokens": 700,
            "tool_choice": "required",
        },
    )


async def connect_mcp_with_retry(
    tool: ConnectableMCPTool,
    *,
    attempts: int,
    initial_backoff_seconds: float,
) -> None:
    for attempt in range(1, attempts + 1):
        try:
            await tool.connect(reset=attempt > 1)
            return
        except ToolException:
            if attempt == attempts:
                raise
            delay = min(initial_backoff_seconds * (2 ** (attempt - 1)), 5.0)
            logger.warning(
                "Sundae MCP startup connection failed on attempt %s/%s; "
                "retrying in %.1f seconds",
                attempt,
                attempts,
                delay,
                exc_info=True,
            )
            await asyncio.sleep(delay)


def create_app(settings: Settings | None = None) -> Any:
    settings = settings or get_settings()
    configure("ops-agent")

    sundae_http_client = httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(20, read=120),
    )
    uninstrument_httpx_client(sundae_http_client)
    sundae_tools = MCPStreamableHTTPTool(
        name="sundae_tools",
        url=settings.normalized_sundae_mcp_url,
        description=(
            "Deterministic sundae menu, availability, quote, and submission tools. "
            "Do not submit orders from the operations specialist."
        ),
        parse_tool_results=parse_sundae_result,
        header_provider=inject_trace_headers,
        http_client=sundae_http_client,
    )
    agent = create_ops_agent(settings, sundae_tools)
    agent_card = create_agent_card(settings)
    handler = DefaultRequestHandler(
        agent_executor=SundaeOpsExecutor(agent, sundae_tools),
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
    )

    async def health(_: Any) -> JSONResponse:
        return JSONResponse(
            {
                "status": "ok",
                "version": settings.app_version,
                "model": settings.openai_chat_model,
                "model_auth_mode": settings.openai_auth_mode.value,
            }
        )

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette):
        async with sundae_http_client:
            await connect_mcp_with_retry(
                sundae_tools,
                attempts=settings.mcp_startup_attempts,
                initial_backoff_seconds=settings.mcp_startup_backoff_seconds,
            )
            try:
                yield
            finally:
                await sundae_tools.close()

    app = Starlette(
        routes=[
            Route("/healthz", health),
            Mount("/metrics", create_metrics_app()),
            *create_agent_card_routes(agent_card),
            *create_jsonrpc_routes(handler, "/"),
        ],
        lifespan=lifespan,
    )
    return instrument_asgi(app)
