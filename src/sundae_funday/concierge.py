"""Minimal FastAPI concierge that uses MCP and A2A."""

import contextlib
import json
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal, Self

from agent_framework import (
    Agent,
    AgentResponse,
    FunctionInvocationContext,
    MiddlewareTermination,
    function_middleware,
    tool,
)
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from sundae_funday.clients import OpsAgentClient, call_mcp_tool, extract_json_object
from sundae_funday.model_client import (
    OpenAIAuthMode,
    create_openai_chat_client,
    model_enabled,
    validate_openai_auth,
)
from sundae_funday.telemetry import configure, create_metrics_app, instrument_asgi

RouterCall = Callable[[str, dict[str, Any]], Awaitable[Any]]
OpsCall = Callable[[str, str], Awaitable[str]]

ROUTER_INSTRUCTIONS = """
You route chat turns for a slim sundae shop concierge demo.
Call capture_chat_plan exactly once.
Choose route="menu" for menu, flavors, toppings, sizes, or price questions.
Choose route="quote" for building, pricing, or revising a sundae draft. Extract
size, flavors, sauce, toppings, and requested_ready_in_minutes when available.
Choose route="operations" for inventory, low stock, wait time, availability,
what is easy to make now, or anything that needs an operations specialist.
For route="operations", copy the user's intent into operations_question so it is
self contained.
Choose route="general" only for greetings or capability questions.
Do not claim an order is submitted or confirmed.
""".strip()

WRITER_INSTRUCTIONS = """
Write one concise customer-facing reply using the latest message, the routing
plan, and authoritative tool results.
Never invent menu, price, stock, ETA, or order facts.
If a draft exists, call it a draft and tell the customer to use Confirm.
Use plain punctuation and do not use em dashes.
""".strip()

SIZE_HINTS = {"mini": "MINI", "classic": "CLASSIC", "deluxe": "DELUXE"}
FLAVOR_HINTS = {
    "vanilla bean": "VANILLA",
    "vanilla": "VANILLA",
    "chocolate": "CHOCOLATE",
    "strawberry": "STRAWBERRY",
    "mint chip": "MINT_CHIP",
    "mint": "MINT_CHIP",
    "coffee": "COFFEE",
}
SAUCE_HINTS = {
    "hot fudge": "HOT_FUDGE",
    "caramel": "CARAMEL",
    "strawberry drizzle": "STRAWBERRY_DRIZZLE",
    "strawberry sauce": "STRAWBERRY_DRIZZLE",
}
TOPPING_HINTS = {
    "cherry": "CHERRY",
    "sprinkles": "SPRINKLES",
    "oreo": "OREO",
    "oreo crumble": "OREO",
    "peanuts": "PEANUTS",
    "banana": "BANANA",
    "whipped cream": "WHIPPED_CREAM",
}
OPS_KEYWORDS = {
    "available",
    "availability",
    "busy",
    "eta",
    "fast",
    "faster",
    "inventory",
    "low stock",
    "out of",
    "quick",
    "ready",
    "running low",
    "stock",
    "tonight",
    "wait",
}
MENU_KEYWORDS = {"menu", "flavors", "toppings", "sizes", "prices", "options"}
ORDER_KEYWORDS = {
    "build",
    "make me",
    "order",
    "quote",
    "sundae",
    "scoop",
    "i want",
    "i would like",
    "can i get",
}
INDEX_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Sundae Funday Concierge</title>
    <style>
      body { font-family: sans-serif; margin: 0; background: #fff8f1; color: #2c1b15; }
      main { max-width: 760px; margin: 0 auto; padding: 2rem 1rem 4rem; }
      h1 { margin-top: 0; }
      #chat {
        background: white;
        border-radius: 12px;
        padding: 1rem;
        min-height: 320px;
        box-shadow: 0 4px 16px rgba(0,0,0,0.08);
      }
      .message { margin: 0 0 1rem; white-space: pre-wrap; }
      .user { font-weight: 700; }
      form { display: grid; gap: 0.75rem; margin-top: 1rem; }
      textarea, input, button {
        font: inherit;
        padding: 0.75rem;
        border-radius: 10px;
        border: 1px solid #d9c7b8;
      }
      textarea { min-height: 84px; }
      .row { display: flex; gap: 0.75rem; }
      .row > * { flex: 1; }
      button { cursor: pointer; background: #6b3df0; color: white; border: none; }
      button.secondary { background: #f0e7ff; color: #4e2ab8; }
      .hint { color: #6a584a; font-size: 0.95rem; }
    </style>
  </head>
  <body>
    <main>
      <h1>Sundae Funday Concierge</h1>
      <p class="hint">
        Ask for the menu, build a sundae draft, or ask an ops question about
        stock and speed.
      </p>
      <div id="chat"></div>
      <form id="chat-form">
        <textarea
          id="message"
          placeholder="Try: classic sundae, vanilla and chocolate, hot fudge, cherry"
        ></textarea>
        <div class="row">
          <input id="customer-name" placeholder="Name for confirmation, optional" />
          <button type="submit">Send</button>
          <button id="confirm" type="button" class="secondary" hidden>
            Confirm draft
          </button>
        </div>
      </form>
    </main>
    <script>
      const chat = document.getElementById('chat');
      const form = document.getElementById('chat-form');
      const messageInput = document.getElementById('message');
      const nameInput = document.getElementById('customer-name');
      const confirmButton = document.getElementById('confirm');
      const sessionKey = 'sundae-funday-session-id';
      const sessionId = localStorage.getItem(sessionKey) || crypto.randomUUID();
      localStorage.setItem(sessionKey, sessionId);

      function addMessage(label, text) {
        const el = document.createElement('p');
        el.className = 'message';
        el.innerHTML =
          `<span class="${label === 'You' ? 'user' : ''}">${label}:</span> ` +
          `${text.replace(/</g, '&lt;')}`;
        chat.appendChild(el);
        chat.scrollTop = chat.scrollHeight;
      }

      async function sendMessage(message) {
        addMessage('You', message);
        const response = await fetch('/api/chat', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({session_id: sessionId, message}),
        });
        const data = await response.json();
        addMessage('Concierge', data.reply);
        confirmButton.hidden = !data.needs_confirmation;
      }

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const message = messageInput.value.trim();
        if (!message) return;
        messageInput.value = '';
        await sendMessage(message);
      });

      confirmButton.addEventListener('click', async () => {
        const response = await fetch('/api/confirm', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({
            session_id: sessionId,
            customer_name: nameInput.value.trim() || undefined,
          }),
        });
        const data = await response.json();
        addMessage('Concierge', data.reply);
        confirmButton.hidden = true;
      });

      addMessage(
        'Concierge',
        'Hi. I can show the menu, build a draft, or ask the ops specialist ' +
        'about inventory and speed.',
      );
    </script>
  </body>
</html>
"""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_version: str = "0.1.0"
    sundae_mcp_url: str = "http://sundae-mcp:8101/mcp/"
    ops_agent_url: str = "http://ops-agent:8202"
    openai_base_url: str = ""
    openai_chat_model: str = ""
    openai_auth_mode: OpenAIAuthMode = OpenAIAuthMode.API_KEY
    openai_api_key: str | None = None
    agent_http_timeout_seconds: float = 60

    @property
    def normalized_sundae_mcp_url(self) -> str:
        return f"{self.sundae_mcp_url.rstrip('/')}/"

    @property
    def model_is_enabled(self) -> bool:
        return model_enabled(self.openai_base_url, self.openai_chat_model)

    @model_validator(mode="after")
    def require_auth_if_enabled(self) -> Self:
        if self.model_is_enabled:
            validate_openai_auth(self.openai_auth_mode, self.openai_api_key)
        return self


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ConfirmRequest(BaseModel):
    session_id: str = Field(min_length=1)
    customer_name: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    needs_confirmation: bool = False
    draft_id: str | None = None
    source: Literal["menu", "quote", "operations", "general"]


class ConfirmResponse(BaseModel):
    session_id: str
    reply: str
    order: dict[str, Any]


class RoutingPlan(BaseModel):
    route: Literal["menu", "quote", "operations", "general"]
    size: str | None = None
    flavors: list[str] = Field(default_factory=list)
    sauce: str | None = None
    toppings: list[str] = Field(default_factory=list)
    requested_ready_in_minutes: int | None = Field(default=None, ge=1)
    operations_question: str | None = None

    @field_validator("flavors", "toppings", mode="before")
    @classmethod
    def normalize_list(cls, value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]


@dataclass(slots=True)
class PendingDraft:
    draft_id: str
    idempotency_key: str
    quote: dict[str, Any]


@dataclass(slots=True)
class SessionState:
    history: list[tuple[str, str]] = field(default_factory=list)
    pending_draft: PendingDraft | None = None


@dataclass(slots=True)
class ToolObservation:
    name: str
    arguments: Any
    result: Any


class SessionStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._sessions: dict[str, SessionState] = {}

    def get(self, session_id: str) -> SessionState:
        with self._lock:
            return self._sessions.setdefault(session_id, SessionState())


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _conversation_context(history: list[tuple[str, str]]) -> str:
    if not history:
        return "No prior conversation."
    lines: list[str] = []
    for user_message, reply in history[-6:]:
        lines.append(f"Customer: {user_message}")
        lines.append(f"Concierge: {reply}")
    return "\n".join(lines)


def extract_tool_observations(response: AgentResponse[Any]) -> list[ToolObservation]:
    calls: dict[str, tuple[str, Any]] = {}
    observations: list[ToolObservation] = []
    for message in response.messages:
        for content in message.contents:
            if content.type == "function_call" and content.call_id:
                calls[content.call_id] = (
                    content.name or "unknown_tool",
                    content.arguments,
                )
            elif content.type == "function_result" and content.call_id:
                name, arguments = calls.get(content.call_id, ("unknown_tool", None))
                observations.append(
                    ToolObservation(
                        name=name,
                        arguments=arguments,
                        result=content.result,
                    )
                )
    return observations


@function_middleware
async def return_tool_result(
    context: FunctionInvocationContext,
    call_next: Callable[[], Awaitable[None]],
) -> None:
    await call_next()
    raise MiddlewareTermination(result=context.result)


def heuristic_plan(message: str) -> RoutingPlan:
    lower = message.lower().strip()
    size = next((value for key, value in SIZE_HINTS.items() if key in lower), None)
    flavors = _extract_terms(lower, FLAVOR_HINTS)
    sauce = _extract_first(lower, SAUCE_HINTS)
    toppings = _extract_terms(lower, TOPPING_HINTS)
    requested_ready_in_minutes = _extract_ready_time(lower)
    if any(keyword in lower for keyword in OPS_KEYWORDS):
        return RoutingPlan(
            route="operations",
            operations_question=message,
            requested_ready_in_minutes=requested_ready_in_minutes,
        )
    if any(keyword in lower for keyword in MENU_KEYWORDS):
        return RoutingPlan(route="menu")
    if (
        any(keyword in lower for keyword in ORDER_KEYWORDS)
        or size is not None
        or flavors
        or sauce is not None
        or toppings
    ):
        return RoutingPlan(
            route="quote",
            size=size,
            flavors=flavors,
            sauce=sauce,
            toppings=toppings,
            requested_ready_in_minutes=requested_ready_in_minutes,
        )
    return RoutingPlan(route="general")


def _extract_terms(message: str, mapping: dict[str, str]) -> list[str]:
    candidates: list[tuple[int, int, str]] = []
    for raw, normalized in mapping.items():
        start = message.find(raw)
        if start >= 0:
            candidates.append((start, -len(raw), normalized))
    matched: list[str] = []
    seen: set[str] = set()
    for _, _, normalized in sorted(candidates):
        if normalized not in seen:
            matched.append(normalized)
            seen.add(normalized)
    return matched


def _extract_first(message: str, mapping: dict[str, str]) -> str | None:
    terms = _extract_terms(message, mapping)
    return terms[0] if terms else None


def _extract_ready_time(message: str) -> int | None:
    match = re.search(r"(\d+)\s*(minute|min)", message)
    if not match:
        return None
    return int(match.group(1))


def render_menu_reply(menu: dict[str, Any]) -> str:
    sizes = ", ".join(
        f"{item['name']} {item['price_display']}" for item in menu.get("sizes", [])
    )
    flavors = ", ".join(item["name"] for item in menu.get("flavors", []))
    toppings = ", ".join(item["name"] for item in menu.get("toppings", [])[:4])
    return (
        f"Sizes: {sizes}. Flavors: {flavors}. Popular toppings include {toppings}. "
        "Tell me a size, flavors, sauce, and toppings and I will draft it."
    )


def render_quote_reply(result: dict[str, Any]) -> str:
    status = result.get("status")
    if status == "needs_clarification":
        return str(result.get("message", "I need a little more detail for that draft."))
    if status == "unavailable":
        items = ", ".join(
            item["name"]
            for item in result.get("unavailable_items", [])
            if isinstance(item, dict)
        )
        detail = f" Unavailable right now: {items}." if items else ""
        return f"I cannot draft that exactly as requested.{detail}"
    quote = result.get("quote")
    order = result.get("order")
    if not isinstance(quote, dict) or not isinstance(order, dict):
        return "I prepared a draft. Use Confirm if it looks right."
    flavors = ", ".join(item["name"] for item in order.get("flavors", []))
    sauce = order.get("sauce")
    sauce_text = f" with {sauce['name']}" if isinstance(sauce, dict) else ""
    toppings = ", ".join(item["name"] for item in order.get("toppings", []))
    toppings_text = f" plus {toppings}" if toppings else ""
    eta = quote.get("eta_minutes")
    eta_text = f" It should take about {eta} minutes." if isinstance(eta, int) else ""
    if quote.get("requested_ready_in_minutes") is not None:
        if quote.get("can_meet_requested_time"):
            eta_text += " That fits your requested timing."
        else:
            eta_text += " That is slower than your requested timing."
    return (
        f"Draft ready: {order['size']['name']} with {flavors}"
        f"{sauce_text}{toppings_text}. "
        f"Total {quote['total_display']}.{eta_text} Use Confirm to submit it."
    )


def render_general_reply() -> str:
    return (
        "I can show the menu, build a draft, or ask the ops specialist about "
        "inventory and speed."
    )


def build_writer_prompt(
    context: str,
    message: str,
    plan: RoutingPlan,
    tool_result: Any,
    *,
    needs_confirmation: bool,
) -> str:
    return (
        f"Conversation context:\n{context}\n\n"
        f"Current customer message:\n{message}\n\n"
        f"Routing plan:\n{_json(plan.model_dump(mode='json'))}\n\n"
        f"Needs confirmation:\n{_json({'needs_confirmation': needs_confirmation})}\n\n"
        f"Authoritative result:\n{_json(tool_result)}"
    )


class ConciergeRuntime:
    def __init__(
        self,
        settings: Settings,
        *,
        mcp_call: RouterCall | None = None,
        ops_call: OpsCall | None = None,
    ) -> None:
        self.settings = settings
        self._mcp_call_override = mcp_call
        self._ops_call_override = ops_call
        self._ops_client = (
            None
            if ops_call is not None
            else OpsAgentClient(
                settings.ops_agent_url,
                settings.agent_http_timeout_seconds,
            )
        )
        self._store = SessionStore()
        self._writer = self.create_writer() if settings.model_is_enabled else None
        self._router = self.create_router() if settings.model_is_enabled else None

    async def close(self) -> None:
        if self._ops_client is not None:
            await self._ops_client.close()

    async def _mcp_call(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._mcp_call_override is not None:
            return await self._mcp_call_override(name, arguments)
        return await call_mcp_tool(
            self.settings.normalized_sundae_mcp_url,
            name,
            arguments,
        )

    async def _ops_call(self, session_id: str, question: str) -> str:
        if self._ops_call_override is not None:
            return await self._ops_call_override(session_id, question)
        if self._ops_client is None:
            raise RuntimeError("Ops agent client is not configured")
        return await self._ops_client.ask(session_id, question)

    def create_writer(self, client: Any | None = None) -> Agent:
        model_client = client or create_openai_chat_client(
            model=self.settings.openai_chat_model,
            base_url=self.settings.openai_base_url,
            auth_mode=self.settings.openai_auth_mode,
            api_key=self.settings.openai_api_key,
        )
        return Agent(
            client=model_client,
            name="ConciergeWriter",
            instructions=WRITER_INSTRUCTIONS,
            default_options={
                "temperature": 0.35,
                "max_tokens": 500,
                "tool_choice": "none",
            },
        )

    def create_router(self, client: Any | None = None) -> Agent:
        @tool(schema=RoutingPlan)
        async def capture_chat_plan(
            route: Literal["menu", "quote", "operations", "general"],
            size: str | None = None,
            flavors: list[str] | None = None,
            sauce: str | None = None,
            toppings: list[str] | None = None,
            requested_ready_in_minutes: int | None = None,
            operations_question: str | None = None,
        ) -> str:
            return _json(
                {
                    "route": route,
                    "size": size,
                    "flavors": flavors or [],
                    "sauce": sauce,
                    "toppings": toppings or [],
                    "requested_ready_in_minutes": requested_ready_in_minutes,
                    "operations_question": operations_question,
                }
            )

        model_client = client or create_openai_chat_client(
            model=self.settings.openai_chat_model,
            base_url=self.settings.openai_base_url,
            auth_mode=self.settings.openai_auth_mode,
            api_key=self.settings.openai_api_key,
            middleware=[return_tool_result],
        )
        return Agent(
            client=model_client,
            name="ConciergeRouter",
            instructions=ROUTER_INSTRUCTIONS,
            tools=[capture_chat_plan],
            default_options={
                "temperature": 0,
                "max_tokens": 500,
                "tool_choice": "required",
            },
        )

    async def plan_turn(self, message: str, context: str) -> RoutingPlan:
        if self._router is None:
            return heuristic_plan(message)
        prompt = (
            f"Conversation context:\n{context}\n\nCurrent customer message:\n{message}"
        )
        prompts = [
            prompt,
            (
                f"{prompt}\n\n"
                "Your previous response did not call capture_chat_plan. Call it "
                "exactly once now and do not answer with text."
            ),
        ]
        for current_prompt in prompts:
            response = await self._router.run(current_prompt)
            captured = [
                observation
                for observation in extract_tool_observations(response)
                if observation.name == "capture_chat_plan"
            ]
            if len(captured) != 1:
                continue
            arguments = captured[0].arguments
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    continue
            if not isinstance(arguments, dict):
                continue
            try:
                return RoutingPlan.model_validate(arguments)
            except ValueError:
                continue
        return heuristic_plan(message)

    async def write_reply(
        self,
        prompt: str,
        fallback: str,
    ) -> str:
        if self._writer is None:
            return fallback
        prompts = [
            prompt,
            (
                f"{prompt}\n\n"
                "Your previous response was empty. Return one concise customer "
                "reply now. Do not call tools."
            ),
        ]
        for current_prompt in prompts:
            response = await self._writer.run(current_prompt)
            text = (response.text or "").strip()
            if text:
                return text
        return fallback

    async def chat(self, session_id: str, message: str) -> ChatResponse:
        state = self._store.get(session_id)
        context = _conversation_context(state.history)
        plan = await self.plan_turn(message, context)
        fallback = render_general_reply()
        authoritative: Any = {"reply": fallback}

        if plan.route == "menu":
            authoritative = await self._mcp_call("list_menu", {})
            fallback = render_menu_reply(authoritative)
        elif plan.route == "quote":
            authoritative = await self._mcp_call(
                "quote_order",
                {
                    "session_id": session_id,
                    "size": plan.size or "CLASSIC",
                    "flavors": plan.flavors,
                    "sauce": plan.sauce,
                    "toppings": plan.toppings,
                    "requested_ready_in_minutes": plan.requested_ready_in_minutes,
                },
            )
            fallback = render_quote_reply(authoritative)
            if (
                isinstance(authoritative, dict)
                and authoritative.get("status") == "ready"
                and authoritative.get("draft_created")
                and isinstance(authoritative.get("draft_id"), str)
            ):
                state.pending_draft = PendingDraft(
                    draft_id=str(authoritative["draft_id"]),
                    idempotency_key=uuid.uuid4().hex,
                    quote=authoritative,
                )
        elif plan.route == "operations":
            response_text = await self._ops_call(
                session_id,
                plan.operations_question or message,
            )
            try:
                authoritative = extract_json_object(response_text)
                fallback = render_quote_reply(authoritative)
            except RuntimeError:
                authoritative = {"text": response_text}
                fallback = response_text
        else:
            authoritative = {"capabilities": ["menu", "quote", "operations", "confirm"]}

        needs_confirmation = state.pending_draft is not None
        reply = await self.write_reply(
            build_writer_prompt(
                context,
                message,
                plan,
                authoritative,
                needs_confirmation=needs_confirmation,
            ),
            fallback,
        )
        state.history.append((message, reply))
        return ChatResponse(
            session_id=session_id,
            reply=reply,
            needs_confirmation=needs_confirmation,
            draft_id=(state.pending_draft.draft_id if state.pending_draft else None),
            source=plan.route,
        )

    async def confirm(
        self,
        session_id: str,
        customer_name: str | None = None,
    ) -> ConfirmResponse:
        state = self._store.get(session_id)
        pending = state.pending_draft
        if pending is None:
            raise RuntimeError("There is no draft waiting for confirmation")
        result = await self._mcp_call(
            "submit_order",
            {
                "draft_id": pending.draft_id,
                "session_id": session_id,
                "idempotency_key": pending.idempotency_key,
                "customer_name": customer_name or "Guest",
            },
        )
        state.pending_draft = None
        reply = (
            f"Order {result['order_id']} is submitted for {result['customer_name']}. "
            f"Pickup in about {result['pickup_eta_minutes']} minutes."
        )
        state.history.append(("confirm", reply))
        return ConfirmResponse(session_id=session_id, reply=reply, order=result)


async def root() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


def create_app(settings: Settings | None = None) -> Any:
    settings = settings or Settings()
    configure("concierge")
    runtime = ConciergeRuntime(settings)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(title="Sundae Funday Concierge", lifespan=lifespan)
    app.mount("/metrics", create_metrics_app())

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return await root()

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": settings.app_version,
            "model_enabled": settings.model_is_enabled,
        }

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest) -> ChatResponse:
        try:
            return await runtime.chat(request.session_id, request.message)
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/confirm", response_model=ConfirmResponse)
    async def confirm(request: ConfirmRequest) -> ConfirmResponse:
        try:
            return await runtime.confirm(request.session_id, request.customer_name)
        except RuntimeError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    return instrument_asgi(app)
