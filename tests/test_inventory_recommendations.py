import json
from unittest.mock import AsyncMock

import pytest

from sundae_funday.concierge import ConciergeRuntime, Settings
from sundae_funday.concierge.presentation import render_special_reply
from sundae_funday.concierge.routing import special_order_plan
from sundae_funday.shop import InMemorySundaeShop


@pytest.mark.asyncio
async def test_surprise_uses_live_stock_and_only_confirmation_consumes_it() -> None:
    shop = InMemorySundaeShop()
    for index in range(16):
        quote = shop.quote_order(
            session_id="seed", size="MINI", flavors=["VANILLA"], sauce="HOT_FUDGE"
        )
        shop.submit_order(
            draft_id=quote["draft_id"], session_id="seed", idempotency_key=str(index)
        )
    before = shop.check_availability()
    ops_calls: list[str] = []

    async def mcp_call(name: str, arguments: dict) -> dict:
        return getattr(shop, name)(**arguments)

    async def ops_call(session_id: str, question: str) -> str:
        request = json.loads(question.removeprefix("SUNDAE_OPS_REQUEST "))
        ops_calls.append(request["operation"])
        return json.dumps(shop.check_availability(**request["arguments"]))

    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""),
        mcp_call=mcp_call,
        ops_call=ops_call,
    )
    surprise = await runtime.chat("customer", "Surprise me")

    assert surprise.source == "surprise"
    assert surprise.needs_confirmation
    assert "Caramel" in surprise.reply
    assert "Hot Fudge" not in surprise.reply
    assert shop.check_availability() == before
    assert ops_calls == ["inventory_special", "verify_fulfillment"]

    confirmed = await runtime.confirm("customer")

    assert confirmed.order["order"]["size"]["sku"] == "CLASSIC"
    assert [item["sku"] for item in confirmed.order["order"]["flavors"]] == [
        "CHOCOLATE",
        "CHOCOLATE",
    ]
    inventory = shop.check_availability()
    assert (
        next(
            item["remaining"]
            for item in inventory["flavors"]
            if item["sku"] == "CHOCOLATE"
        )
        == 12
    )
    assert (
        next(
            item["remaining"]
            for item in inventory["sauces"]
            if item["sku"] == "CARAMEL"
        )
        == 14
    )


def test_recommendation_combines_last_scoops_instead_of_overallocating() -> None:
    inventory = InMemorySundaeShop().check_availability()
    inventory["flavors"] = [
        {"name": "Vanilla Bean", "remaining": 1, "available": True},
        {"name": "Chocolate", "remaining": 1, "available": True},
        {"name": "Mint Chip", "remaining": 0, "available": True},
    ]

    plan = special_order_plan(inventory)

    assert plan.flavors == ["Vanilla Bean", "Chocolate"]
    assert "a scoop each of Vanilla Bean and Chocolate" in render_special_reply(
        inventory
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["flavors", "sauces", "toppings"])
async def test_surprise_does_not_quote_a_recipe_without_enough_stock(
    category: str,
) -> None:
    inventory = InMemorySundaeShop().check_availability()
    for item in inventory[category]:
        item["remaining"] = 0
    if category == "flavors":
        inventory[category][0]["remaining"] = 1
    mcp_call = AsyncMock()
    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""),
        mcp_call=mcp_call,
        ops_call=AsyncMock(return_value=json.dumps(inventory)),
    )

    with pytest.raises(RuntimeError, match="Ops inventory response"):
        await runtime.chat("customer", "Surprise me")

    mcp_call.assert_not_called()
    with pytest.raises(RuntimeError, match="no sundae waiting"):
        await runtime.confirm("customer")
