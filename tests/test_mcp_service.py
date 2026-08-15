import pytest

from sundae_funday.mcp_service import mcp


@pytest.mark.asyncio
async def test_mcp_tool_names_and_schemas_are_stable() -> None:
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    assert list(tools) == [
        "list_menu",
        "check_availability",
        "quote_order",
        "submit_order",
    ]
    assert tools["list_menu"].inputSchema["properties"] == {}
    assert list(tools["check_availability"].inputSchema["properties"]) == [
        "flavors",
        "sauce",
        "toppings",
    ]
    assert list(tools["quote_order"].inputSchema["properties"]) == [
        "session_id",
        "size",
        "flavors",
        "sauce",
        "toppings",
        "requested_ready_in_minutes",
    ]
    assert tools["quote_order"].inputSchema["required"] == ["session_id"]
    assert (
        tools["quote_order"].inputSchema["properties"]["size"]["default"] == "CLASSIC"
    )
    assert list(tools["submit_order"].inputSchema["properties"]) == [
        "draft_id",
        "session_id",
        "idempotency_key",
        "customer_name",
    ]
    assert tools["submit_order"].inputSchema["required"] == [
        "draft_id",
        "session_id",
        "idempotency_key",
    ]
    assert (
        tools["submit_order"].inputSchema["properties"]["customer_name"]["default"]
        == "Guest"
    )
