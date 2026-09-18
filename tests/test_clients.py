import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from agent_framework import MCPStreamableHTTPTool

from sundae_funday.ops_agent import Settings, create_app
from sundae_funday.protocol import (
    OpsAgentClient,
    extract_json_object,
    parse_mcp_result,
)
from sundae_funday.shop import InMemorySundaeShop


class FakeResult:
    def __init__(
        self,
        *,
        is_error: bool,
        structured: dict | None,
        content: list[object],
    ):
        self.isError = is_error
        self.structuredContent = structured
        self.content = content


def test_parse_mcp_result_prefers_structured_content() -> None:
    result = FakeResult(is_error=False, structured={"result": {"ok": True}}, content=[])

    assert parse_mcp_result(result, "quote_order") == {"ok": True}


def test_parse_mcp_result_loads_text_json() -> None:
    result = FakeResult(
        is_error=False,
        structured=None,
        content=[SimpleNamespace(text='{"status":"ok"}')],
    )

    assert parse_mcp_result(result, "quote_order") == {"status": "ok"}


def test_parse_mcp_result_raises_on_error() -> None:
    result = FakeResult(is_error=True, structured=None, content=[])

    with pytest.raises(RuntimeError):
        parse_mcp_result(result, "quote_order")


def test_extract_json_object_finds_embedded_object() -> None:
    text = 'Here you go {"status":"ready","draft_id":"d1"} thanks'

    assert extract_json_object(text)["draft_id"] == "d1"


@pytest.mark.asyncio
async def test_ops_client_injects_current_trace_headers_and_owns_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sundae_funday.protocol.inject_trace_headers",
        lambda: {"traceparent": "00-current-trace"},
    )
    client = OpsAgentClient("http://ops-agent:8202")
    request = httpx.Request("POST", "http://ops-agent:8202")

    await client._inject_trace_headers(request)
    await client.close()

    assert request.headers["traceparent"] == "00-current-trace"
    assert client._http_client.is_closed


@pytest.mark.asyncio
async def test_concurrent_ops_calls_use_independent_tasks_across_replicas(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_INSTRUMENTATION", "false")
    monkeypatch.setattr(
        "sundae_funday.protocol.inject_trace_headers",
        lambda: {"traceparent": "00-current-trace"},
    )
    shop = InMemorySundaeShop()
    active = peak = 0

    async def call_tool(self, name: str, **arguments) -> str:
        nonlocal active, peak
        assert name == "check_availability"
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return json.dumps(shop.check_availability(**arguments))

    monkeypatch.setattr(MCPStreamableHTTPTool, "call_tool", call_tool)
    settings = Settings(
        openai_base_url="http://unused/v1",
        openai_chat_model="unused",
        openai_api_key="unused",
        ops_demo_work_seconds=0,
    )
    transports = [httpx.ASGITransport(app=create_app(settings)) for _ in range(2)]
    requests: list[dict] = []

    async def send(request: httpx.Request) -> httpx.Response:
        assert request.headers["traceparent"] == "00-current-trace"
        replica = len(requests) % 2
        if request.method == "POST":
            requests.append(json.loads(request.content))
        return await transports[replica].handle_async_request(request)

    client = OpsAgentClient("http://ops-agent:8202")
    await client._http_client.aclose()
    client._http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(send),
        event_hooks={"request": [client._inject_trace_headers]},
    )
    question = 'SUNDAE_OPS_REQUEST {"operation":"inventory_special"}'
    try:
        # Repeated customer IDs must not link to task state on an earlier pod.
        replies = await asyncio.gather(
            *(client.ask("same-customer", question) for _ in range(6))
        )
        await client.ask("same-customer", question)
        with pytest.raises(RuntimeError, match="failed to complete"):
            await client.ask(
                "same-customer", 'SUNDAE_OPS_REQUEST {"operation":"invalid"}'
            )
    finally:
        await client.close()

    assert peak > 1
    assert all(json.loads(reply)["flavors"] for reply in replies)
    assert len(requests) == 8
    for request in requests:
        message = request["params"]["message"]
        assert not message.get("taskId")
        assert not message.get("referenceTaskIds")
        assert not message.get("contextId")
