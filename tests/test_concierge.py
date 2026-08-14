import json

import pytest

from concierge import (
    INDEX_HTML,
    ConciergeRuntime,
    RoutingPlan,
    Settings,
    display_order_number,
    heuristic_plan,
)


@pytest.mark.asyncio
async def test_concierge_quote_flow_uses_confirm() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        calls.append((name, arguments))
        if name == "quote_order":
            return {
                "status": "ready",
                "draft_created": True,
                "draft_id": "draft-123",
                "order": {
                    "size": {"name": "Classic Sundae"},
                    "flavors": [{"name": "Vanilla Bean"}, {"name": "Chocolate"}],
                    "sauce": {"name": "Hot Fudge"},
                    "toppings": [{"name": "Cherry"}],
                },
                "quote": {
                    "total_display": "$7.00",
                    "eta_minutes": 8,
                    "requested_ready_in_minutes": None,
                    "can_meet_requested_time": True,
                },
            }
        if name == "submit_order":
            return {
                "order_id": "sundae-1",
                "customer_name": arguments["customer_name"],
                "pickup_eta_minutes": 8,
            }
        raise AssertionError(name)

    settings = Settings(
        openai_base_url="",
        openai_chat_model="",
        ops_agent_url="http://ops-agent:8202",
        sundae_mcp_url="http://sundae-mcp:8101/mcp/",
    )

    async def fake_ops(session_id: str, question: str) -> str:
        assert session_id == "session-1"
        assert '"operation":"verify_fulfillment"' in question
        return json.dumps({"can_make_now": True, "requested_items": []})

    runtime = ConciergeRuntime(
        settings,
        mcp_call=fake_mcp,
        ops_call=fake_ops,
    )

    chat = await runtime.chat(
        "session-1",
        "build me a classic sundae with vanilla and chocolate",
    )
    confirm = await runtime.confirm("session-1", "Ava")

    assert chat.needs_confirmation is True
    assert confirm.order["order_id"] == "sundae-1"
    assert confirm.reply.startswith("Order 1 is submitted")
    assert calls[0][0] == "quote_order"
    assert calls[1][0] == "submit_order"


@pytest.mark.asyncio
async def test_concierge_operations_route_uses_ops_agent() -> None:
    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        raise AssertionError(f"unexpected MCP call: {name} {arguments}")

    async def fake_ops(session_id: str, question: str) -> str:
        assert session_id == "session-1"
        assert "running low" in question
        return "Mint chip is low, but vanilla and chocolate are fine."

    settings = Settings(openai_base_url="", openai_chat_model="")
    runtime = ConciergeRuntime(settings, mcp_call=fake_mcp, ops_call=fake_ops)

    response = await runtime.chat("session-1", "What are you running low on tonight?")

    assert response.source == "operations"
    assert "Mint chip" in response.reply


def test_heuristic_plan_extracts_quote_details() -> None:
    plan = heuristic_plan(
        "Build me a deluxe sundae with vanilla, chocolate, mint chip, "
        "hot fudge, and a cherry in 12 minutes"
    )

    assert plan == RoutingPlan(
        route="quote",
        size="DELUXE",
        flavors=["VANILLA", "CHOCOLATE", "MINT_CHIP"],
        sauce="HOT_FUDGE",
        toppings=["CHERRY"],
        requested_ready_in_minutes=12,
        operations_question=None,
    )


def test_page_keeps_confirmation_action_visible() -> None:
    assert 'id="confirm" type="button" disabled' in INDEX_HTML
    assert "Pick toppings, flavors..." in INDEX_HTML
    assert "Confirm and submit order" in INDEX_HTML
    assert 'id="confirm" type="button" hidden' not in INDEX_HTML
    assert "form.requestSubmit(sendButton)" in INDEX_HTML
    assert "line-height: 50px" in INDEX_HTML


def test_heuristic_plan_prefers_quote_when_order_hints_present() -> None:
    plan = heuristic_plan(
        "Classic sundae with vanilla, chocolate, how fast can it be ready?"
    )

    assert plan.route == "quote"
    assert plan.size == "CLASSIC"
    assert "VANILLA" in plan.flavors


def test_heuristic_plan_handles_ops_question_after_quote() -> None:
    plan = heuristic_plan("why does it take 10 minutes to make?")
    assert plan.route == "general"


@pytest.mark.asyncio
async def test_concierge_surprise_me_generates_sundae() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        calls.append((name, arguments))
        if name == "list_menu":
            return {
                "shop_name": "Sundae Funday",
                "sizes": [],
                "flavors": [],
                "toppings": [],
            }
        if name == "quote_order":
            raw_flavors: object = arguments.get("flavors", [])
            flavors = [] if not isinstance(raw_flavors, list) else raw_flavors
            size_name = "Classic Sundae"
            for s in ["CLASSIC", "MINI", "DELUXE"]:
                if s == arguments.get("size"):
                    size_name = {
                        "CLASSIC": "Classic Sundae",
                        "MINI": "Mini Sundae",
                    }.get(s, "Deluxe Sundae")
                    break
            return {
                "status": "ready",
                "draft_created": True,
                "draft_id": "draft-surprise",
                "order": {
                    "size": {"name": size_name},
                    "flavors": [{"name": f} for f in list(flavors)],
                    "sauce": {"name": "Hot Fudge"},
                    "toppings": [{"name": "Cherry"}, {"name": "Sprinkles"}],
                },
                "quote": {
                    "total_display": "$6.50",
                    "eta_minutes": 8,
                    "requested_ready_in_minutes": None,
                    "can_meet_requested_time": True,
                },
            }
        if name == "submit_order":
            return {
                "order_id": "sundae-2",
                "customer_name": arguments["customer_name"],
                "pickup_eta_minutes": 8,
            }
        raise AssertionError(name)

    settings = Settings(
        openai_base_url="",
        openai_chat_model="",
        ops_agent_url="http://ops-agent:8202",
        sundae_mcp_url="http://sundae-mcp:8101/mcp/",
    )

    async def fake_ops(session_id: str, question: str) -> str:
        assert session_id == "session-surprise"
        assert '"operation":"verify_fulfillment"' in question
        return json.dumps({"can_make_now": True, "requested_items": []})

    runtime = ConciergeRuntime(
        settings,
        mcp_call=fake_mcp,
        ops_call=fake_ops,
    )

    response = await runtime.chat("session-surprise", "Surprise me!")

    assert response.needs_confirmation is True
    assert calls[0][0] == "quote_order"


def test_heuristic_plan_surprise_message() -> None:
    plan = heuristic_plan("Surprise me — pick whatever you're feeling today!")
    assert plan.route == "surprise"


def test_heuristic_plan_routes_specials_to_operations() -> None:
    plan = heuristic_plan("Got any specials today?")

    assert plan.route == "operations"
    assert plan.operations_question == "Got any specials today?"


@pytest.mark.asyncio
async def test_special_uses_highest_inventory_from_ops() -> None:
    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        raise AssertionError(f"unexpected MCP call: {name} {arguments}")

    async def fake_ops(session_id: str, question: str) -> str:
        assert session_id == "special-session"
        assert '"operation":"inventory_special"' in question
        return json.dumps(
            {
                "can_make_now": True,
                "flavors": [
                    {
                        "name": "Vanilla Bean",
                        "remaining": 18,
                        "available": True,
                    },
                    {"name": "Mint Chip", "remaining": 6, "available": True},
                ],
                "sauces": [
                    {"name": "Hot Fudge", "remaining": 16, "available": True},
                    {"name": "Caramel", "remaining": 15, "available": True},
                ],
                "toppings": [
                    {
                        "name": "Rainbow Sprinkles",
                        "remaining": 20,
                        "available": True,
                    },
                    {
                        "name": "Whipped Cream",
                        "remaining": 13,
                        "available": True,
                    },
                ],
            }
        )

    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""),
        mcp_call=fake_mcp,
        ops_call=fake_ops,
    )

    response = await runtime.chat("special-session", "Got any specials today?")

    assert response.source == "operations"
    assert "Vanilla Bean" in response.reply
    assert "Rainbow Sprinkles" in response.reply
    assert "Whipped Cream" in response.reply


@pytest.mark.asyncio
async def test_customer_can_accept_inventory_special_in_follow_up() -> None:
    quote_arguments: dict[str, object] = {}

    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        assert name == "quote_order"
        quote_arguments.update(arguments)
        return {
            "status": "ready",
            "draft_created": True,
            "draft_id": "draft-special",
            "order": {
                "size": {"name": "Classic Sundae"},
                "flavors": [
                    {"name": "Vanilla Bean"},
                    {"name": "Vanilla Bean"},
                ],
                "sauce": {"name": "Hot Fudge"},
                "toppings": [
                    {"name": "Rainbow Sprinkles"},
                    {"name": "Whipped Cream"},
                ],
            },
            "quote": {
                "total_display": "$7.75",
                "eta_minutes": 8,
                "requested_ready_in_minutes": None,
                "can_meet_requested_time": True,
            },
        }

    async def fake_ops(session_id: str, question: str) -> str:
        if '"operation":"inventory_special"' in question:
            return json.dumps(
                {
                    "flavors": [
                        {
                            "name": "Vanilla Bean",
                            "remaining": 18,
                            "available": True,
                        }
                    ],
                    "sauces": [
                        {
                            "name": "Hot Fudge",
                            "remaining": 16,
                            "available": True,
                        }
                    ],
                    "toppings": [
                        {
                            "name": "Rainbow Sprinkles",
                            "remaining": 20,
                            "available": True,
                        },
                        {
                            "name": "Whipped Cream",
                            "remaining": 13,
                            "available": True,
                        },
                    ],
                }
            )
        assert '"operation":"verify_fulfillment"' in question
        return json.dumps({"can_make_now": True, "requested_items": []})

    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""),
        mcp_call=fake_mcp,
        ops_call=fake_ops,
    )

    special = await runtime.chat("special-follow-up", "Any specials today?")
    quote = await runtime.chat(
        "special-follow-up",
        "The classic sundae is what I want",
    )

    assert "two scoops of Vanilla Bean" in special.reply
    assert quote.needs_confirmation is True
    assert quote_arguments["size"] == "CLASSIC"
    assert quote_arguments["flavors"] == ["Vanilla Bean", "Vanilla Bean"]
    assert quote_arguments["sauce"] == "Hot Fudge"
    assert quote_arguments["toppings"] == [
        "Rainbow Sprinkles",
        "Whipped Cream",
    ]


@pytest.mark.asyncio
async def test_quote_details_accumulate_across_chat_turns() -> None:
    calls: list[dict[str, object]] = []

    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        assert name == "quote_order"
        calls.append(arguments)
        flavors = arguments["flavors"]
        if not isinstance(flavors, list) or len(flavors) < 2:
            return {
                "status": "needs_clarification",
                "message": "Add 1 more flavor choice for Classic Sundae.",
            }
        return {
            "status": "needs_clarification",
            "message": "Ready for test assertions.",
        }

    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""),
        mcp_call=fake_mcp,
        ops_call=None,
    )

    await runtime.chat("multi-turn", "I want a classic sundae")
    await runtime.chat("multi-turn", "vanilla with hot fudge")
    await runtime.chat("multi-turn", "chocolate and sprinkles")

    assert calls[-1]["size"] == "CLASSIC"
    assert calls[-1]["flavors"] == ["VANILLA", "CHOCOLATE"]
    assert calls[-1]["sauce"] == "HOT_FUDGE"
    assert calls[-1]["toppings"] == ["SPRINKLES"]


@pytest.mark.asyncio
async def test_quote_requires_ops_fulfillment_approval() -> None:
    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        assert name == "quote_order"
        return {
            "status": "ready",
            "draft_created": True,
            "draft_id": "draft-unavailable",
            "order": {
                "size": {"name": "Mini Sundae"},
                "flavors": [{"name": "Vanilla Bean"}],
                "sauce": None,
                "toppings": [{"name": "Banana"}],
            },
            "quote": {
                "total_display": "$5.50",
                "eta_minutes": 7,
                "requested_ready_in_minutes": None,
            },
        }

    async def fake_ops(session_id: str, question: str) -> str:
        return json.dumps(
            {
                "can_make_now": False,
                "requested_items": [
                    {"name": "Banana", "available": False},
                ],
            }
        )

    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""),
        mcp_call=fake_mcp,
        ops_call=fake_ops,
    )

    response = await runtime.chat(
        "unavailable-session",
        "Make me a mini vanilla sundae with banana",
    )

    assert response.needs_confirmation is False
    assert "Ops Scoop cannot fulfill" in response.reply
    assert "Banana" in response.reply


def test_display_order_number_uses_first_two_digits() -> None:
    assert display_order_number("sundae-55b8472e") == "55"
    assert display_order_number("sundae-a8b3c7") == "83"
