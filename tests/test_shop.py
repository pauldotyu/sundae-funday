from shop import InMemorySundaeShop


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


def test_availability_snapshot_marks_low_stock() -> None:
    shop = InMemorySundaeShop()
    availability = shop.check_availability()

    assert availability["can_make_now"] is True
    assert isinstance(availability["low_stock"], list)
