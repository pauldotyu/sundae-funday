import pytest

from sundae_funday.concierge import (
    ConciergeRuntime,
    RoutingPlan,
    Settings,
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
    runtime = ConciergeRuntime(settings, mcp_call=fake_mcp)

    chat = await runtime.chat(
        "session-1",
        "build me a classic sundae with vanilla and chocolate",
    )
    confirm = await runtime.confirm("session-1", "Ava")

    assert chat.needs_confirmation is True
    assert confirm.order["order_id"] == "sundae-1"
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
