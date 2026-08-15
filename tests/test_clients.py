from types import SimpleNamespace

import httpx
import pytest

from sundae_funday.protocol import (
    OpsAgentClient,
    extract_json_object,
    parse_mcp_result,
)


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
