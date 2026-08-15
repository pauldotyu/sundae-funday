from typing import Any

import pytest

from sundae_funday.shop import InMemorySundaeShop


def test_menu_preserves_catalog_order_and_response_shape() -> None:
    menu = InMemorySundaeShop().list_menu()

    assert list(menu) == [
        "shop_name",
        "currency",
        "confirm_flow",
        "sizes",
        "flavors",
        "sauces",
        "toppings",
    ]
    assert [item["sku"] for item in menu["sizes"]] == ["MINI", "CLASSIC", "DELUXE"]
    assert [item["sku"] for item in menu["flavors"]] == [
        "VANILLA",
        "CHOCOLATE",
        "STRAWBERRY",
        "MINT_CHIP",
        "COFFEE",
    ]
    assert [item["sku"] for item in menu["sauces"]] == [
        "HOT_FUDGE",
        "CARAMEL",
        "STRAWBERRY_DRIZZLE",
    ]
    assert [item["sku"] for item in menu["toppings"]] == [
        "CHERRY",
        "SPRINKLES",
        "OREO",
        "GRAHAM_CRACKERS",
        "PEANUTS",
        "BANANA",
        "WHIPPED_CREAM",
    ]


def test_aliases_and_repeated_ingredients_preserve_request_counts() -> None:
    shop = InMemorySundaeShop()

    availability = shop.check_availability(
        flavors=["mint", "mint chocolate chip"],
        sauce="hotfudge",
        toppings=["oreo crumble", "whip"],
    )

    assert availability["can_make_now"] is True
    assert availability["unknown_items"] == []
    assert availability["requested_items"] == [
        {
            "category": "flavors",
            "sku": "MINT_CHIP",
            "name": "Mint Chip",
            "requested": 2,
            "remaining": 6,
            "available": True,
        },
        {
            "category": "sauces",
            "sku": "HOT_FUDGE",
            "name": "Hot Fudge",
            "requested": 1,
            "remaining": 16,
            "available": True,
        },
        {
            "category": "toppings",
            "sku": "OREO",
            "name": "Oreo Crumble",
            "requested": 1,
            "remaining": 11,
            "available": True,
        },
        {
            "category": "toppings",
            "sku": "WHIPPED_CREAM",
            "name": "Whipped Cream",
            "requested": 1,
            "remaining": 13,
            "available": True,
        },
    ]


def test_quote_creates_ready_draft_with_integer_cents() -> None:
    shop = InMemorySundaeShop()

    result = shop.quote_order(
        session_id="session-1",
        size="classic",
        flavors=["vanilla", "chocolate"],
        sauce="hot fudge",
        toppings=["cherry", "oreo"],
        requested_ready_in_minutes=10,
    )

    assert result["status"] == "ready"
    assert result["draft_created"] is True
    assert result["quote"]["total_cents"] == 775
    assert result["quote"]["total_display"] == "$7.75"
    assert result["quote"]["can_meet_requested_time"] is True


def test_quote_reports_unavailable_items() -> None:
    shop = InMemorySundaeShop()

    first = shop.quote_order(
        session_id="session-1",
        size="classic",
        flavors=["mint chip", "mint chip"],
        toppings=["banana"],
    )
    shop.submit_order(
        draft_id=first["draft_id"],
        session_id="session-1",
        idempotency_key="key-1",
    )
    second = shop.quote_order(
        session_id="session-2",
        size="deluxe",
        flavors=["mint chip", "mint chip", "mint chip"],
        toppings=["banana", "banana", "banana", "banana", "banana"],
    )

    assert second["status"] == "unavailable"
    assert second["unavailable_items"]


def test_submit_order_replays_same_idempotency_key() -> None:
    shop = InMemorySundaeShop()
    draft = shop.quote_order(
        session_id="session-1",
        size="mini",
        flavors=["vanilla"],
    )

    first = shop.submit_order(
        draft_id=draft["draft_id"],
        session_id="session-1",
        idempotency_key="same-key",
        customer_name="Ava",
    )
    second = shop.submit_order(
        draft_id=draft["draft_id"],
        session_id="session-1",
        idempotency_key="same-key",
        customer_name="Ava",
    )

    assert first["order_id"] == second["order_id"]
    assert second["idempotent_replay"] is True


def test_submit_order_mutates_inventory_only_after_confirmation() -> None:
    shop = InMemorySundaeShop()
    before = shop.check_availability(flavors=["vanilla", "vanilla"])
    draft = shop.quote_order(
        session_id="session-1",
        size="classic",
        flavors=["vanilla", "vanilla"],
        sauce="caramel",
        toppings=["cherry", "cherry"],
    )
    after_quote = shop.check_availability(flavors=["vanilla", "vanilla"])

    shop.submit_order(
        draft_id=draft["draft_id"],
        session_id="session-1",
        idempotency_key="inventory-key",
    )
    after_submit = shop.check_availability(
        flavors=["vanilla", "vanilla"],
        sauce="caramel",
        toppings=["cherry", "cherry"],
    )

    assert before["requested_items"][0]["remaining"] == 18
    assert after_quote["requested_items"][0]["remaining"] == 18
    assert after_submit["requested_items"][0]["remaining"] == 16
    assert after_submit["requested_items"][1]["remaining"] == 14
    assert after_submit["requested_items"][2]["remaining"] == 8


def test_submit_order_enforces_draft_ownership_and_single_submission() -> None:
    shop = InMemorySundaeShop()
    draft = shop.quote_order(
        session_id="owner",
        size="mini",
        flavors=["coffee"],
    )

    with pytest.raises(RuntimeError, match="Draft does not belong to this session"):
        shop.submit_order(
            draft_id=draft["draft_id"],
            session_id="other",
            idempotency_key="wrong-owner",
        )

    shop.submit_order(
        draft_id=draft["draft_id"],
        session_id="owner",
        idempotency_key="first-submit",
    )
    with pytest.raises(RuntimeError, match="Draft already submitted"):
        shop.submit_order(
            draft_id=draft["draft_id"],
            session_id="owner",
            idempotency_key="second-submit",
        )


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        (
            {"size": "large", "flavors": ["vanilla"]},
            {
                "status": "needs_clarification",
                "message": "Pick mini, classic, or deluxe for the sundae size.",
                "options": ["Mini Sundae", "Classic Sundae", "Deluxe Sundae"],
            },
        ),
        (
            {"size": "mini", "flavors": []},
            {
                "status": "needs_clarification",
                "message": "Pick at least one ice cream flavor before I can price it.",
            },
        ),
        (
            {"size": "classic", "flavors": ["vanilla"]},
            {
                "status": "needs_clarification",
                "message": "Add 1 more flavor choice for Classic Sundae.",
            },
        ),
        (
            {"size": "mini", "flavors": ["pistachio"]},
            {
                "status": "needs_clarification",
                "message": "I could not match every requested item to the menu.",
                "unknown_items": ["pistachio"],
            },
        ),
    ],
)
def test_quote_clarification_messages_are_stable(
    arguments: dict[str, Any],
    expected: dict[str, object],
) -> None:
    result = InMemorySundaeShop().quote_order(session_id="session-1", **arguments)

    assert result == expected


def test_availability_snapshot_marks_low_stock() -> None:
    shop = InMemorySundaeShop()
    availability = shop.check_availability()

    assert availability["can_make_now"] is True
    assert isinstance(availability["low_stock"], list)
