"""A2A operations specialist grounded in Sundae MCP tools."""

import contextlib
import json
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any, Self

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
from mcp.types import CallToolResult
from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from sundae_funday.model_client import (
    OpenAIAuthMode,
    create_openai_chat_client,
    validate_openai_auth,
)
from sundae_funday.telemetry import (
    configure,
    create_metrics_app,
    inject_trace_headers,
    instrument_asgi,
    uninstrument_httpx_client,
)

OPS_INSTRUCTIONS = """
You are Ops Scoop, the Sundae Funday operations specialist.
Call a Sundae MCP tool before every answer. Use the narrowest tool that fits.
Use list_menu for sizes, prices, flavors, sauces, and toppings.
Use check_availability for stock, shortages, and what can be made now.
Use quote_order for hypothetical builds, pricing, and ETA questions. When you
need quote_order, pass session_id="ops-agent" and include requested_ready_in_minutes
when the customer gave a time target.
Never call submit_order. Only the concierge confirm action can submit an order.
Never invent stock, price, ETA, or menu facts.
If the user asks about an exact sundae build, call quote_order and return only
that tool result as JSON. For broader questions, answer briefly and cite the
relevant tool facts.
""".strip()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_version: str = "0.1.0"
    ops_agent_public_base_url: str = "http://ops-agent:8202"
    sundae_mcp_url: str = "http://sundae-mcp:8101/mcp/"
    openai_base_url: str = ""
    openai_chat_model: str = ""
    openai_auth_mode: OpenAIAuthMode = OpenAIAuthMode.API_KEY
    openai_api_key: str | None = None

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
        return f"{self.sundae_mcp_url.rstrip('/')}/"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def parse_sundae_result(result: CallToolResult) -> str:
    if result.structuredContent is not None:
        return json.dumps(
            result.structuredContent,
            separators=(",", ":"),
            sort_keys=True,
        )
    for content in result.content:
        text = getattr(content, "text", None)
        if text:
            return text
    raise RuntimeError("Sundae MCP returned no result")


@function_middleware
async def return_sundae_result(
    context: FunctionInvocationContext,
    call_next: Callable[[], Awaitable[None]],
) -> None:
    await call_next()
    raise MiddlewareTermination(result=context.result)


def extract_tool_results(response: AgentResponse[Any]) -> list[str]:
    results: list[str] = []
    for message in response.messages:
        for content in message.contents:
            if content.type != "function_result":
                continue
            result = content.result
            if isinstance(result, str):
                results.append(result)
            elif result is not None:
                results.append(json.dumps(result, separators=(",", ":")))
    return results


async def run_with_required_tool(
    agent: Agent,
    query: Any,
    session: AgentSession,
) -> list[str]:
    prompts = [
        query,
        (
            f"Original request:\n{query}\n\n"
            "Your previous response did not call a tool. Call exactly one "
            "Sundae MCP tool now and do not answer with text."
        ),
    ]
    for prompt in prompts:
        response = await agent.run(prompt, session=session, stream=False)
        results = extract_tool_results(response)
        if results:
            return results
    return []


class SundaeOpsExecutor(A2AExecutor):
    def __init__(self, agent: Agent) -> None:
        super().__init__(agent)
        self._ops_agent = agent

    async def _run(
        self,
        query: Any,
        session: AgentSession,
        updater: TaskUpdater,
    ) -> None:
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
                url=f"{settings.ops_agent_public_base_url.rstrip('/')}/",
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
        agent_executor=SundaeOpsExecutor(agent),
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
        async with sundae_http_client, sundae_tools:
            yield

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
