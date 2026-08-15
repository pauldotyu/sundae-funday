from mcp_service import mcp


def test_mcp_tool_names_and_schemas_are_stable() -> None:
    tools = {tool.name: tool for tool in mcp._tool_manager.list_tools()}

    assert list(tools) == [
        "list_menu",
        "check_availability",
        "quote_order",
        "submit_order",
    ]
    assert tools["list_menu"].parameters["properties"] == {}
    assert list(tools["check_availability"].parameters["properties"]) == [
        "flavors",
        "sauce",
        "toppings",
    ]
    assert list(tools["quote_order"].parameters["properties"]) == [
        "session_id",
        "size",
        "flavors",
        "sauce",
        "toppings",
        "requested_ready_in_minutes",
    ]
    assert tools["quote_order"].parameters["required"] == ["session_id"]
    assert tools["quote_order"].parameters["properties"]["size"]["default"] == "CLASSIC"
    assert list(tools["submit_order"].parameters["properties"]) == [
        "draft_id",
        "session_id",
        "idempotency_key",
        "customer_name",
    ]
    assert tools["submit_order"].parameters["required"] == [
        "draft_id",
        "session_id",
        "idempotency_key",
    ]
    assert (
        tools["submit_order"].parameters["properties"]["customer_name"]["default"]
        == "Guest"
    )
