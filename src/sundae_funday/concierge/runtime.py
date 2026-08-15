"""Concierge routing and runtime orchestration."""

import json
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from agent_framework import (
    Agent,
    AgentResponse,
    FunctionInvocationContext,
    MiddlewareTermination,
    function_middleware,
    tool,
)

from sundae_funday.agent_runtime import run_agent_attempts
from sundae_funday.catalog import (
    SIZES,
    SURPRISE_FLAVOR_SKUS,
    SURPRISE_SAUCE_SKUS,
    SURPRISE_SIZE_SKUS,
    SURPRISE_TOPPING_SKUS,
)
from sundae_funday.concierge.api import (
    ChatResponse,
    ConfirmResponse,
    RoutingPlan,
    Settings,
)
from sundae_funday.concierge.presentation import (
    build_writer_prompt,
    compact_json,
    display_order_number,
    ops_request,
    render_fulfillment_failure,
    render_general_reply,
    render_menu_reply,
    render_quote_reply,
    render_special_reply,
)
from sundae_funday.concierge.routing import (
    heuristic_plan,
    is_special_request,
    merge_order_plan,
    special_order_plan,
    unwrap_tool_result,
)
from sundae_funday.concierge.state import (
    PendingDraft,
    SessionState,
    SessionStore,
    conversation_context,
    extract_tool_observations,
)
from sundae_funday.model_client import create_openai_chat_client
from sundae_funday.protocol import OpsAgentClient, call_mcp_tool, extract_json_object

RouterCall = Callable[[str, dict[str, Any]], Awaitable[Any]]
OpsCall = Callable[[str, str], Awaitable[str]]

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
For "surprise me", "pick whatever", "choose for me", use route="surprise".
Do not choose operations for customer service questions about ice cream prep time,
flavor explanations, toppings, flavors, or menu items. These belong in the current
conversation. For general capability questions or greetings, use route="general".
Never choose operations for complaints, exclamations, or confusion about timing
or readiness. These are customer service messages for the current conversation.
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


@dataclass(slots=True)
class TurnResult:
    plan: RoutingPlan
    authoritative: Any
    fallback: str
    use_writer: bool = True


@function_middleware
async def return_tool_result(
    context: FunctionInvocationContext,
    call_next: Callable[[], Awaitable[None]],
) -> None:
    await call_next()
    raise MiddlewareTermination(result=context.result)


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
            return compact_json(
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
        if is_special_request(message):
            return RoutingPlan(route="operations", operations_question=message)
        router = self._router
        if router is None:
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

        async def execute(current_prompt: str) -> AgentResponse:
            return await router.run(current_prompt)

        def extract(response: AgentResponse) -> RoutingPlan | None:
            captured = [
                observation
                for observation in extract_tool_observations(response)
                if observation.name == "capture_chat_plan"
            ]
            if len(captured) != 1:
                return None
            arguments = captured[0].arguments
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    return None
            if not isinstance(arguments, dict):
                return None
            try:
                return RoutingPlan.model_validate(arguments)
            except ValueError:
                return None

        return await run_agent_attempts(prompts, execute, extract) or heuristic_plan(
            message
        )

    async def write_reply(
        self,
        prompt: str,
        fallback: str,
    ) -> str:
        writer = self._writer
        if writer is None:
            return fallback
        prompts = [
            prompt,
            (
                f"{prompt}\n\n"
                "Your previous response was empty. Return one concise customer "
                "reply now. Do not call tools."
            ),
        ]

        async def execute(current_prompt: str) -> AgentResponse:
            return await writer.run(current_prompt)

        def extract(response: AgentResponse) -> str | None:
            text = (response.text or "").strip()
            return text or None

        return await run_agent_attempts(prompts, execute, extract) or fallback

    async def verify_fulfillment(
        self,
        session_id: str,
        quote: dict[str, Any],
    ) -> dict[str, Any]:
        order = quote.get("order")
        if not isinstance(order, dict):
            raise RuntimeError("The sundae quote is missing order details")
        sauce = order.get("sauce")
        response_text = await self._ops_call(
            session_id,
            ops_request(
                "verify_fulfillment",
                flavors=[
                    item.get("sku") or item.get("name")
                    for item in order.get("flavors", [])
                    if isinstance(item, dict)
                ],
                sauce=(
                    sauce.get("sku") or sauce.get("name")
                    if isinstance(sauce, dict)
                    else None
                ),
                toppings=[
                    item.get("sku") or item.get("name")
                    for item in order.get("toppings", [])
                    if isinstance(item, dict)
                ],
            ),
        )
        verification = unwrap_tool_result(extract_json_object(response_text))
        if not isinstance(verification.get("can_make_now"), bool):
            raise RuntimeError(
                "Ops Scoop did not return a structured fulfillment decision"
            )
        return verification

    async def _finalize_quote(
        self,
        session_id: str,
        state: SessionState,
        result: dict[str, Any],
    ) -> str:
        state.pending_draft = None
        fallback = render_quote_reply(result)
        ready = (
            result.get("status") == "ready"
            and result.get("draft_created")
            and isinstance(result.get("draft_id"), str)
        )
        if not ready:
            return fallback
        verification = await self.verify_fulfillment(session_id, result)
        if not verification["can_make_now"]:
            return render_fulfillment_failure(verification)
        state.pending_draft = PendingDraft(
            draft_id=str(result["draft_id"]),
            idempotency_key=uuid.uuid4().hex,
            quote=result,
        )
        return fallback

    async def _quote(
        self,
        session_id: str,
        plan: RoutingPlan,
    ) -> dict[str, Any]:
        result = await self._mcp_call(
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
        if not isinstance(result, dict):
            raise RuntimeError("Sundae MCP returned an invalid quote")
        return result

    async def _handle_menu(
        self,
        _: str,
        __: str,
        ___: SessionState,
        plan: RoutingPlan,
    ) -> TurnResult:
        result = await self._mcp_call("list_menu", {})
        if not isinstance(result, dict):
            raise RuntimeError("Sundae MCP returned an invalid menu")
        return TurnResult(plan, result, render_menu_reply(result))

    async def _handle_quote(
        self,
        session_id: str,
        _: str,
        state: SessionState,
        plan: RoutingPlan,
    ) -> TurnResult:
        merged = merge_order_plan(state.order_plan, plan)
        state.order_plan = merged
        result = await self._quote(session_id, merged)
        fallback = await self._finalize_quote(session_id, state, result)
        return TurnResult(merged, result, fallback, use_writer=False)

    async def _handle_operations(
        self,
        session_id: str,
        message: str,
        state: SessionState,
        plan: RoutingPlan,
    ) -> TurnResult:
        special_request = is_special_request(message)
        response_text = await self._ops_call(
            session_id,
            (
                ops_request("inventory_special")
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
            low = response_text.lower()
            is_failure = any(
                keyword in low
                for keyword in ("unable to", "cannot ", "failed to", "unavailable")
            ) or (
                "sorry" in low
                and any(keyword in low for keyword in ("agent", "ops", "error"))
            )
            if is_failure:
                fallback = (
                    "That one is a bit outside my wheelhouse. "
                    "Your sundae is ready. "
                    "Please confirm when you would like to proceed!"
                )
                authoritative = {"capabilities": ["menu", "quote"]}
            else:
                authoritative = {"text": response_text}
                fallback = response_text
        if special_request:
            state.order_plan = special_order_plan(authoritative)
        return TurnResult(
            plan,
            authoritative,
            fallback,
            use_writer=not special_request,
        )

    async def _handle_surprise(
        self,
        session_id: str,
        _: str,
        state: SessionState,
        plan: RoutingPlan,
    ) -> TurnResult:
        selected_size = random.choice(SURPRISE_SIZE_SKUS)
        scoop_count = SIZES[selected_size].included_scoops
        selected = RoutingPlan(
            route="quote",
            size=selected_size,
            flavors=random.sample(SURPRISE_FLAVOR_SKUS, scoop_count),
            sauce=random.choice(SURPRISE_SAUCE_SKUS),
            toppings=random.sample(SURPRISE_TOPPING_SKUS, 2),
        )
        result = await self._quote(session_id, selected)
        fallback = await self._finalize_quote(session_id, state, result)
        state.order_plan = selected
        return TurnResult(plan, result, fallback)

    async def _handle_general(
        self,
        _: str,
        __: str,
        ___: SessionState,
        plan: RoutingPlan,
    ) -> TurnResult:
        authoritative = {"capabilities": ["menu", "quote", "operations", "confirm"]}
        return TurnResult(plan, authoritative, render_general_reply())

    async def chat(self, session_id: str, message: str) -> ChatResponse:
        state = self._store.get(session_id)
        context = conversation_context(state.history)
        plan = await self.plan_turn(message, context)
        handlers = {
            "menu": self._handle_menu,
            "quote": self._handle_quote,
            "operations": self._handle_operations,
            "surprise": self._handle_surprise,
            "general": self._handle_general,
        }
        turn = await handlers[plan.route](session_id, message, state, plan)
        needs_confirmation = state.pending_draft is not None
        reply = (
            await self.write_reply(
                build_writer_prompt(
                    context,
                    message,
                    turn.plan,
                    turn.authoritative,
                    needs_confirmation=needs_confirmation,
                ),
                turn.fallback,
            )
            if turn.use_writer
            else turn.fallback
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
        if not isinstance(result, dict):
            raise RuntimeError("Sundae MCP returned an invalid order")
        state.pending_draft = None
        state.order_plan = None
        order_number = display_order_number(result.get("order_id"))
        reply = (
            f"Order {order_number} is submitted for {result['customer_name']}. "
            f"Pickup in about {result['pickup_eta_minutes']} minutes."
        )
        state.history.append(("confirm", reply))
        return ConfirmResponse(session_id=session_id, reply=reply, order=result)
