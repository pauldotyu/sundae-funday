"""Minimal FastAPI concierge that uses MCP and A2A."""

import contextlib
import json
import random
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Literal, Self

import httpx
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
from opentelemetry import trace
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from clients import OpsAgentClient, call_mcp_tool, extract_json_object
from model_client import (
    OpenAIAuthMode,
    create_openai_chat_client,
    model_enabled,
    validate_openai_auth,
)
from telemetry import configure, create_metrics_app, instrument_asgi

RouterCall = Callable[[str, dict[str, Any]], Awaitable[Any]]
OpsCall = Callable[[str, str], Awaitable[str]]
tracer = trace.get_tracer("sundae-funday.concierge")

ROUTER_INSTRUCTIONS = """
You route chat turns for a slim sundae shop concierge demo.
Call capture_chat_plan exactly once.
Choose route="menu" for menu, flavors, toppings, sizes, or price questions.
Choose route="quote" for building, pricing, or revising a sundae. Extract
size, flavors, sauce, toppings, and requested_ready_in_minutes when available.
Choose route="operations" only for inventory checks (low stock, running low) or
availability checks that require external tool data you cannot answer directly.
Choose route="operations" for specials, promotions, or recommendations based on
what the shop has the most inventory available to sell.
For "surprise me", "pick whatever", "choose for me" — use route="surprise".
Do not choose operations for customer service questions about ice cream prep time,
flavor explanations, toppings, flavors, or menu items -- these belong in the current
conversation. For general capability questions or greetings, use route="general".
Never choose operations for complaints, exclamations, or confusion about timing
or readiness (for example "10 mins?!?", "but it said ready!", "how long though?") --
these are customer service messages best answered in the current conversation.
Do not claim an order is submitted or confirmed.
""".strip()

WRITER_INSTRUCTIONS = """
Write one concise customer-facing reply using the latest message, the routing
plan, and authoritative tool results.
Never invent menu, price, stock, ETA, or order facts.
For specials, recommend a complete sundae using the highest-inventory flavor,
sauce, and toppings from the authoritative inventory result.
If a sundae is ready, tell the customer it is ready and ask them to use Confirm.
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
    "graham crackers": "GRAHAM_CRACKERS",
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
MENU_KEYWORDS = {
    "menu",
    "flavors",
    "toppings",
    "sizes",
    "prices",
    "options",
    "cost",
    "how much",
}
SURPRISE_PHRASES = ["surprise me", "pick whatever", "choose for me"]
SPECIAL_PHRASES = [
    "any specials",
    "daily special",
    "on special",
    "special today",
    "specials",
]
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
    <link rel="icon" href="data:," />
    <title>The Sundae Shop</title>
    <style>
      :root {
        color-scheme: light;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont,
          "Segoe UI", sans-serif;
        background: #fffaf3;
        color: #241711;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        background:
          radial-gradient(circle at 10% 5%, #ffdce8 0, transparent 24rem),
          radial-gradient(circle at 92% 8%, #dff8ed 0, transparent 26rem),
          #fffaf3;
      }
      button, textarea, input { font: inherit; }
      button { cursor: pointer; }
      .shell { max-width: 1180px; margin: 0 auto; padding: 2.5rem 1.25rem; }
      header {
        display: flex;
        align-items: flex-end;
        justify-content: space-between;
        gap: 2rem;
        margin-bottom: 1.5rem;
      }
      .eyebrow {
        margin: 0 0 0.4rem;
        color: #9b3f67;
        font-size: 0.78rem;
        font-weight: 800;
        letter-spacing: 0.14em;
        text-transform: uppercase;
      }
      h1 {
        margin: 0;
        font-family: Georgia, "Times New Roman", serif;
        font-size: clamp(2.4rem, 6vw, 4.8rem);
        line-height: 0.94;
        letter-spacing: -0.055em;
      }
      .subtitle {
        max-width: 31rem;
        margin: 0;
        color: #715d53;
        font-size: 1rem;
        line-height: 1.6;
      }
      .layout {
        display: grid;
        grid-template-columns: minmax(0, 1fr) 320px;
        gap: 1rem;
        align-items: stretch;
      }
      .card {
        border: 1px solid rgba(91, 58, 44, 0.12);
        border-radius: 24px;
        background: rgba(255, 255, 255, 0.88);
        box-shadow: 0 24px 70px rgba(92, 54, 37, 0.1);
        backdrop-filter: blur(12px);
      }
      .chat-card {
        display: grid;
        grid-template-rows: auto minmax(340px, 1fr) auto;
        min-height: 640px;
        overflow: hidden;
      }
      .chat-heading {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        padding: 1rem 1.25rem;
        border-bottom: 1px solid #f0e4da;
      }
      .avatar {
        display: grid;
        width: 42px;
        height: 42px;
        place-items: center;
        border-radius: 14px;
        background: #ffe0eb;
        font-size: 1.35rem;
      }
      .chat-heading strong, .chat-heading small { display: block; }
      .chat-heading small { margin-top: 0.12rem; color: #8a756a; }
      #chat {
        display: flex;
        flex-direction: column;
        gap: 0.9rem;
        overflow-y: auto;
        padding: 1.25rem;
      }
      .message {
        max-width: 84%;
        padding: 0.85rem 1rem;
        border-radius: 18px 18px 18px 5px;
        background: #f7efe8;
        color: #39261d;
        line-height: 1.5;
        white-space: pre-wrap;
      }
      .message.user {
        align-self: flex-end;
        border-radius: 18px 18px 5px 18px;
        background: #3c2b73;
        color: white;
      }
      .message-label {
        display: block;
        margin-bottom: 0.2rem;
        font-size: 0.72rem;
        font-weight: 800;
        letter-spacing: 0.06em;
        opacity: 0.65;
        text-transform: uppercase;
      }
      #chat-form {
        display: grid;
        grid-template-columns: 1fr auto;
        gap: 0.7rem;
        padding: 1rem;
        border-top: 1px solid #f0e4da;
        background: #fffdf9;
      }
      textarea, input {
        width: 100%;
        border: 1px solid #dccbc0;
        border-radius: 14px;
        background: white;
        color: #241711;
        outline: none;
      }
      textarea {
        height: 52px;
        min-height: 52px;
        resize: none;
        padding: 0 1rem;
        line-height: 50px;
        overflow: hidden;
      }
      textarea:focus, input:focus {
        border-color: #9b3f67;
        box-shadow: 0 0 0 3px rgba(155, 63, 103, 0.1);
      }
      .send {
        min-width: 92px;
        border: 0;
        border-radius: 14px;
        background: #241711;
        color: white;
        font-weight: 800;
      }
      .side { display: flex; flex-direction: column; gap: 1rem; }
      .panel { padding: 1.25rem; }
      .panel h2 { margin: 0 0 0.35rem; font-size: 1.05rem; }
      .panel-copy { margin: 0; color: #7b675c; font-size: 0.9rem; line-height: 1.45; }
      .status {
        display: flex;
        align-items: center;
        gap: 0.5rem;
        margin: 1.1rem 0;
        padding: 0.75rem;
        border-radius: 14px;
        background: #f7efe8;
        color: #6f5a4f;
        font-size: 0.88rem;
        font-weight: 700;
      }
      .status.ready { background: #e5f8ee; color: #17613c; }
      .dot {
        width: 9px;
        height: 9px;
        border-radius: 50%;
        background: currentColor;
        opacity: 0.7;
      }
      #customer-name { padding: 0.8rem; }
      #confirm {
        width: 100%;
        margin-top: 0.7rem;
        padding: 0.9rem;
        border: 0;
        border-radius: 14px;
        background: #ef4d83;
        color: white;
        font-weight: 850;
      }
      #confirm:disabled {
        cursor: not-allowed;
        background: #eadfd8;
        color: #9a8880;
      }
      .quick-list { display: grid; gap: 0.55rem; margin-top: 1rem; }
      .quick {
        padding: 0.75rem 0.8rem;
        border: 1px solid #ead9cf;
        border-radius: 14px;
        background: white;
        color: #49352b;
        text-align: left;
        line-height: 1.35;
      }
      .quick:hover { border-color: #ef4d83; background: #fff7fa; }
      .error { color: #a1243d; }
      @media (max-width: 820px) {
        header { display: block; }
        .subtitle { margin-top: 1rem; }
        .layout { grid-template-columns: 1fr; }
        .chat-card { min-height: 580px; }
        .side { order: -1; }
      }
      @media (max-width: 520px) {
        .shell { padding: 1.5rem 0.75rem; }
        #chat-form { grid-template-columns: 1fr; }
        .send { min-height: 48px; }
        .message { max-width: 94%; }
      }
    </style>
  </head>
  <body>
    <main class="shell">
      <header>
        <div>
          <p class="eyebrow">AI agent demo</p>
          <h1>Sundae<br />Funday</h1>
        </div>
        <p class="subtitle">
          Build a sundae with a conversational concierge. Shop facts and prices
          come from MCP tools. Operational questions go to a specialist over A2A.
        </p>
      </header>
      <div class="layout">
        <section class="card chat-card" aria-label="Concierge chat">
          <div class="chat-heading">
            <div class="avatar" aria-hidden="true">🍨</div>
            <div>
              <strong>The Sundae Scoop</strong>
              <small>Just scooping along</small>
            </div>
          </div>
          <div id="chat" aria-live="polite"></div>
          <form id="chat-form">
            <textarea
              id="message"
              aria-label="Message"
              placeholder="Describe your sundae..."
            ></textarea>
            <button class="send" type="submit">Send</button>
          </form>
        </section>
        <aside class="side">
          <section class="card panel">
            <h2>Your order</h2>
            <p class="panel-copy">
              Your sundae will appear here. Press Confirm when you're happy with it.
            </p>
            <div id="draft-status" class="status">
              <span class="dot"></span>
              <span>No sundae yet</span>
            </div>
            <input
              id="customer-name"
              aria-label="Customer name"
              placeholder="Name for pickup"
            />
            <button id="confirm" type="button" disabled>
              Pick toppings, flavors...
            </button>
          </section>
          <section class="card panel">
            <h2>Choose one of our favorites</h2>
            <p class="panel-copy">
              Flavors that we can't get enough of.
            </p>
            <div class="quick-list">
              <button class="quick" type="button"
                data-prompt="surprise me">
                Surprise Me! 🍦
              </button>
              <button class="quick" type="button"
                data-prompt="Classic: vanilla, chocolate, hot fudge, Oreo, cherry.">
                The Hot Fudge Classic
              </button>
              <button class="quick" type="button"
                data-prompt="Mini: strawberry, strawberry drizzle, whipped cream.">
                Strawberry Cloud
              </button>
              <button class="quick" type="button"
                data-prompt="Deluxe: vanilla, chocolate, hot fudge, graham crackers.">
                Graham Central Station
              </button>
            </div>
          </section>
        </aside>
      </div>
    </main>
    <script>
      const chat = document.getElementById('chat');
      const form = document.getElementById('chat-form');
      const messageInput = document.getElementById('message');
      const nameInput = document.getElementById('customer-name');
      const confirmButton = document.getElementById('confirm');
      const draftStatus = document.getElementById('draft-status');
      const sendButton = form.querySelector('button');
      const sessionKey = 'sundae-funday-session-id';
      const uuidv4 = () => {
                if (typeof crypto !== 'undefined' && crypto.randomUUID) {
                    return crypto.randomUUID();
                }
        return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
          const r = Math.random() * 16 | 0;
          return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
        });
      };
      const sessionId = localStorage.getItem(sessionKey) || uuidv4();
      localStorage.setItem(sessionKey, sessionId);

      function addMessage(label, text) {
        const bubble = document.createElement('div');
        const heading = document.createElement('span');
        const content = document.createElement('span');
        bubble.className = `message ${label === 'You' ? 'user' : ''}`;
        heading.className = 'message-label';
        heading.textContent = label;
        content.textContent = text.replaceAll('**', '');
        bubble.append(heading, content);
        chat.appendChild(bubble);
        chat.scrollTop = chat.scrollHeight;
      }

      function setDraftState(data) {
        const ready = Boolean(data.needs_confirmation);
        draftStatus.classList.toggle('ready', ready);
        draftStatus.querySelector('span:last-child').textContent = ready
          ? "A custom sundae is waiting \u2014 confirm when you're ready!"
          : "What would you like to build?";
        confirmButton.disabled = !ready;
        confirmButton.textContent = ready
          ? 'Confirm and submit order'
          : 'Pick toppings, flavors...';
      }

      async function sendMessage(message) {
        addMessage('You', message);
        sendButton.disabled = true;
        sendButton.textContent = 'Thinking...';
        try {
          const response = await fetch('/api/chat', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({session_id: sessionId, message}),
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data.detail || 'The request failed.');
          addMessage('Concierge', data.reply);
          setDraftState(data);
        } catch (error) {
          addMessage('Concierge', `Sorry, ${error.message}`);
        } finally {
          sendButton.disabled = false;
          sendButton.textContent = 'Send';
        }
      }

      form.addEventListener('submit', async (event) => {
        event.preventDefault();
        const message = messageInput.value.trim();
        if (!message) return;
        messageInput.value = '';
        await sendMessage(message);
      });

      messageInput.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' || event.isComposing) return;
        event.preventDefault();
        form.requestSubmit(sendButton);
      });

      document.querySelectorAll('.quick').forEach((button) => {
        button.addEventListener('click', () => sendMessage(button.dataset.prompt));
      });

      confirmButton.addEventListener('click', async () => {
        confirmButton.disabled = true;
        confirmButton.textContent = 'Submitting...';
        try {
          const response = await fetch('/api/confirm', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
              session_id: sessionId,
              customer_name: nameInput.value.trim() || undefined,
            }),
          });
          const data = await response.json();
          if (!response.ok) throw new Error(data.detail || 'Confirmation failed.');
          addMessage('Concierge', data.reply);
          setDraftState({needs_confirmation: false});
        } catch (error) {
          addMessage('Concierge', `Sorry, ${error.message}`);
          confirmButton.disabled = false;
          confirmButton.textContent = 'Confirm and submit order';
        }
      });

      addMessage(
        'Concierge',
        'Hi! Pick a complete order on the right, describe your own sundae, or ' +
        'ask me what is available. Your sundae will appear here before you ' +
        'confirm.',
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
    source: Literal["menu", "quote", "operations", "surprise", "general"]


class ConfirmResponse(BaseModel):
    session_id: str
    reply: str
    order: dict[str, Any]


class RoutingPlan(BaseModel):
    route: Literal["menu", "quote", "operations", "surprise", "general"]
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
    order_plan: RoutingPlan | None = None


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
    if _is_special_request(lower):
        return RoutingPlan(route="operations", operations_question=message)
    # Check surprise phrases first — before any other routing logic
    if any(phrase in lower for phrase in [s.lower() for s in SURPRISE_PHRASES]):
        return RoutingPlan(route="surprise")
    size = next((value for key, value in SIZE_HINTS.items() if key in lower), None)
    flavors = _extract_terms(lower, FLAVOR_HINTS)
    sauce = _extract_first(lower, SAUCE_HINTS)
    toppings = _extract_terms(lower, TOPPING_HINTS)
    requested_ready_in_minutes = _extract_ready_time(lower)
    if any(keyword in lower for keyword in OPS_KEYWORDS):
        # If the message looks like an order request despite ops keywords,
        # prefer quote so we can price it for the customer.
        if size is not None or flavors or sauce is not None or toppings:
            return RoutingPlan(
                route="quote",
                size=size,
                flavors=flavors,
                sauce=sauce,
                toppings=toppings,
                requested_ready_in_minutes=requested_ready_in_minutes,
            )
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


def _is_special_request(message: str) -> bool:
    lower = message.lower()
    return any(phrase in lower for phrase in SPECIAL_PHRASES)


def _unwrap_tool_result(result: dict[str, Any]) -> dict[str, Any]:
    nested = result.get("result")
    return nested if isinstance(nested, dict) else result


def _highest_inventory_item(
    inventory: dict[str, Any],
    category: str,
) -> dict[str, Any]:
    items = inventory.get(category)
    if not isinstance(items, list):
        raise RuntimeError(f"Ops inventory response is missing {category}")
    available = [
        item
        for item in items
        if isinstance(item, dict)
        and item.get("available") is True
        and isinstance(item.get("remaining"), int)
    ]
    if not available:
        raise RuntimeError(f"Ops inventory response has no available {category}")
    return max(available, key=lambda item: int(item["remaining"]))


def render_special_reply(result: dict[str, Any]) -> str:
    plan = _special_order_plan(result)
    return (
        "Today's special is a Classic Sundae with "
        f"two scoops of {plan.flavors[0]}, {plan.sauce}, "
        f"{plan.toppings[0]}, and {plan.toppings[1]}. "
    )


def _special_order_plan(result: dict[str, Any]) -> RoutingPlan:
    inventory = _unwrap_tool_result(result)
    flavor = _highest_inventory_item(inventory, "flavors")
    sauce = _highest_inventory_item(inventory, "sauces")
    toppings = inventory.get("toppings")
    if not isinstance(toppings, list):
        raise RuntimeError("Ops inventory response is missing toppings")
    available_toppings = sorted(
        (
            item
            for item in toppings
            if isinstance(item, dict)
            and item.get("available") is True
            and isinstance(item.get("remaining"), int)
        ),
        key=lambda item: int(item["remaining"]),
        reverse=True,
    )
    if len(available_toppings) < 2:
        raise RuntimeError("Ops inventory response needs two available toppings")
    flavor_name = str(flavor["name"])
    return RoutingPlan(
        route="quote",
        size="CLASSIC",
        flavors=[flavor_name, flavor_name],
        sauce=str(sauce["name"]),
        toppings=[
            str(available_toppings[0]["name"]),
            str(available_toppings[1]["name"]),
        ],
    )


def _merge_order_plan(
    existing: RoutingPlan | None,
    update: RoutingPlan,
) -> RoutingPlan:
    if existing is None:
        return update

    flavors = list(existing.flavors)
    for flavor in update.flavors:
        if flavor not in flavors:
            flavors.append(flavor)

    toppings = list(existing.toppings)
    for topping in update.toppings:
        if topping not in toppings:
            toppings.append(topping)

    return RoutingPlan(
        route="quote",
        size=update.size or existing.size,
        flavors=flavors,
        sauce=update.sauce or existing.sauce,
        toppings=toppings,
        requested_ready_in_minutes=(
            update.requested_ready_in_minutes or existing.requested_ready_in_minutes
        ),
    )


def render_fulfillment_failure(result: dict[str, Any]) -> str:
    unavailable = [
        str(item["name"])
        for item in result.get("requested_items", [])
        if isinstance(item, dict) and item.get("available") is False
    ]
    detail = f" Unavailable right now: {', '.join(unavailable)}." if unavailable else ""
    return f"Ops Scoop cannot fulfill that sundae as requested.{detail}"


def display_order_number(order_id: Any) -> str:
    digits = "".join(character for character in str(order_id) if character.isdigit())
    return digits[:2]


def _ops_request(operation: str, **arguments: Any) -> str:
    return "SUNDAE_OPS_REQUEST " + _json(
        {"operation": operation, "arguments": arguments}
    )


def render_menu_reply(menu: dict[str, Any]) -> str:
    sizes = ", ".join(
        f"{item['name']} {item['price_display']}" for item in menu.get("sizes", [])
    )
    flavors = ", ".join(item["name"] for item in menu.get("flavors", []))
    toppings = ", ".join(item["name"] for item in menu.get("toppings", [])[:4])
    return (
        f"Sizes: {sizes}. Flavors: {flavors}. Popular toppings include {toppings}. "
        "Tell me a size, flavors, sauce, and toppings and I will build it."
    )


def render_quote_reply(result: dict[str, Any]) -> str:
    status = result.get("status")
    if status == "needs_clarification":
        return str(
            result.get("message", "I need a little more detail for that sundae.")
        )
    if status == "unavailable":
        items = ", ".join(
            item["name"]
            for item in result.get("unavailable_items", [])
            if isinstance(item, dict)
        )
        detail = f" Unavailable right now: {items}." if items else ""
        return f"I cannot build that exactly as requested.{detail}"
    quote = result.get("quote")
    order = result.get("order")
    if not isinstance(quote, dict) or not isinstance(order, dict):
        return "Your sundae is ready. Use Confirm if it looks right."
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
        f"Your sundae is ready: {order['size']['name']} with {flavors}"
        f"{sauce_text}{toppings_text}. "
        f"Total {quote['total_display']}.{eta_text} Press confirm to submit it."
    )


def render_general_reply() -> str:
    return (
        "I can show the menu, build a sundae, or check inventory for you. "
        "What sounds good?"
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
        self._traced_a2a_client: httpx.AsyncClient | None = None
        self._ops_client = (
            None
            if ops_call is not None
            else OpsAgentClient(
                settings.ops_agent_url,
                settings.agent_http_timeout_seconds,
                self._get_traced_a2a_client(),
            )
        )
        self._store = SessionStore()
        self._writer = self.create_writer() if settings.model_is_enabled else None
        self._router = self.create_router() if settings.model_is_enabled else None

    def _get_traced_a2a_client(self) -> httpx.AsyncClient:
        """Return a shared httpx client instrumented for A2A calls."""
        if self._traced_a2a_client is None or self._traced_a2a_client.is_closed:
            self._traced_a2a_client = httpx.AsyncClient(
                timeout=self.settings.agent_http_timeout_seconds
            )
        return self._traced_a2a_client

    async def close(self) -> None:
        if self._ops_client is not None:
            await self._ops_client.close()
        if (
            self._traced_a2a_client is not None
            and not self._traced_a2a_client.is_closed
        ):
            await self._traced_a2a_client.aclose()

    async def _mcp_call(self, name: str, arguments: dict[str, Any]) -> Any:
        if self._mcp_call_override is not None:
            return await self._mcp_call_override(name, arguments)
        return await call_mcp_tool(
            self.settings.normalized_sundae_mcp_url,
            name,
            arguments,
            self.settings.agent_http_timeout_seconds,
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
            route: Literal["menu", "quote", "operations", "surprise", "general"],
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
        if _is_special_request(message):
            return RoutingPlan(route="operations", operations_question=message)
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

    async def verify_fulfillment(
        self,
        session_id: str,
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        order = quote.get("order")
        if not isinstance(order, dict):
            raise RuntimeError("The sundae quote is missing order details")
        response_text = await self._ops_call(
            session_id,
            _ops_request(
                "verify_fulfillment",
                flavors=[
                    item.get("sku") or item.get("name")
                    for item in order.get("flavors", [])
                    if isinstance(item, dict)
                ],
                sauce=(
                    order["sauce"].get("sku") or order["sauce"].get("name")
                    if isinstance(order.get("sauce"), dict)
                    else None
                ),
                toppings=[
                    item.get("sku") or item.get("name")
                    for item in order.get("toppings", [])
                    if isinstance(item, dict)
                ],
            ),
        )
        verification = _unwrap_tool_result(extract_json_object(response_text))
        if not isinstance(verification.get("can_make_now"), bool):
            raise RuntimeError(
                "Ops Scoop did not return a structured fulfillment decision"
            )
        return verification

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
            plan = _merge_order_plan(state.order_plan, plan)
            state.order_plan = plan
            state.pending_draft = None
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
                verification = await self.verify_fulfillment(
                    session_id,
                    authoritative,
                )
                if verification["can_make_now"]:
                    state.pending_draft = PendingDraft(
                        draft_id=str(authoritative["draft_id"]),
                        idempotency_key=uuid.uuid4().hex,
                        quote=authoritative,
                    )
                else:
                    fallback = render_fulfillment_failure(verification)
        elif plan.route == "operations":
            special_request = _is_special_request(message)
            response_text = await self._ops_call(
                session_id,
                (
                    _ops_request("inventory_special")
                    if special_request
                    else plan.operations_question or message
                ),
            )
            try:
                authoritative = extract_json_object(response_text)
                fallback = (
                    render_special_reply(authoritative)
                    if special_request
                    else render_quote_reply(authoritative)
                )
            except RuntimeError:
                # Non-JSON result from ops agent. If it looks like a genuine
                # failure (the ops agent couldn't answer), use a friendly
                # fallback instead of leaking the raw error through the writer.
                low = response_text.lower()
                is_failure = any(
                    kw in low
                    for kw in ("unable to", "cannot ", "failed to", "unavailable")
                ) or (
                    "sorry" in low
                    and any(kw in low for kw in ("agent", "ops", "error"))
                )
                if is_failure:
                    fallback = (
                        "That one is a bit outside my wheelhouse. "
                        "Your sundae is ready. "
                        "Please press Confirm when you would like to proceed!"
                    )
                    authoritative = {"capabilities": ["menu", "quote"]}
                else:
                    authoritative = {"text": response_text}
                    fallback = response_text
            if special_request and isinstance(authoritative, dict):
                state.order_plan = _special_order_plan(authoritative)
        elif plan.route == "surprise":
            available_flavors = [
                "VANILLA",
                "CHOCOLATE",
                "STRAWBERRY",
                "MINT_CHIP",
                "COFFEE",
            ]
            available_sauces = ["HOT_FUDGE", "CARAMEL", "STRAWBERRY_DRIZZLE"]
            available_toppings = [
                "CHERRY",
                "SPRINKLES",
                "OREO",
                "PEANUTS",
                "WHIPPED_CREAM",
                "BANANA",
            ]
            size_options = {"CLASSIC": "Classic Sundae", "MINI": "Mini Sundae"}
            selected_size = random.choice(list(size_options.keys()))
            count = 1 if selected_size == "MINI" else 2
            chosen_flavors = random.sample(
                available_flavors, min(count, len(available_flavors))
            )
            chosen_sauce = random.choice(available_sauces)
            chosen_toppings = random.sample(available_toppings, 2)
            result = await self._mcp_call(
                "quote_order",
                {
                    "session_id": session_id,
                    "size": selected_size,
                    "flavors": chosen_flavors,
                    "sauce": chosen_sauce,
                    "toppings": chosen_toppings,
                },
            )
            fallback = render_quote_reply(result)
            if (
                isinstance(result, dict)
                and result.get("status") == "ready"
                and result.get("draft_created")
                and isinstance(result.get("draft_id"), str)
            ):
                verification = await self.verify_fulfillment(session_id, result)
                if verification["can_make_now"]:
                    state.pending_draft = PendingDraft(
                        draft_id=str(result["draft_id"]),
                        idempotency_key=uuid.uuid4().hex,
                        quote=result,
                    )
                else:
                    fallback = render_fulfillment_failure(verification)
            authoritative = result
            state.order_plan = RoutingPlan(
                route="quote",
                size=selected_size,
                flavors=chosen_flavors,
                sauce=chosen_sauce,
                toppings=chosen_toppings,
            )
        else:
            authoritative = {"capabilities": ["menu", "quote", "operations", "confirm"]}

        needs_confirmation = state.pending_draft is not None
        if plan.route == "quote" or (
            plan.route == "operations" and _is_special_request(message)
        ):
            reply = fallback
        else:
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
            raise RuntimeError("There is no sundae waiting for confirmation")
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
        state.order_plan = None
        order_number = display_order_number(result.get("order_id"))
        reply = (
            f"Order {order_number} is submitted for {result['customer_name']}. "
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
        with tracer.start_as_current_span("concierge.chat") as span:
            span.set_attribute("conversation.id", request.session_id)
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", "SundaeConcierge")
            span.set_attribute("chat.message.length", len(request.message))
            try:
                response = await runtime.chat(request.session_id, request.message)
            except RuntimeError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            span.set_attribute("chat.route", response.source)
            span.set_attribute("chat.needs_confirmation", response.needs_confirmation)
            return response

    @app.post("/api/confirm", response_model=ConfirmResponse)
    async def confirm(request: ConfirmRequest) -> ConfirmResponse:
        with tracer.start_as_current_span("concierge.confirm") as span:
            span.set_attribute("conversation.id", request.session_id)
            try:
                return await runtime.confirm(request.session_id, request.customer_name)
            except RuntimeError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error

    return instrument_asgi(app)
