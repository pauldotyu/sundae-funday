import asyncio
import logging
from unittest.mock import MagicMock

import pytest
from a2a.server.tasks import TaskUpdater
from agent_framework import Agent, AgentSession, MCPStreamableHTTPTool
from agent_framework.exceptions import ToolException
from mcp.types import CallToolResult
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from sundae_funday.ops_agent import (
    Settings,
    SundaeOpsExecutor,
    connect_mcp_with_retry,
    parse_sundae_result,
)


class FakeMCPTool:
    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls: list[bool] = []

    async def connect(self, *, reset: bool = False) -> None:
        self.calls.append(reset)
        if len(self.calls) <= self.failures:
            raise ToolException("MCP server is not ready")


@pytest.mark.asyncio
async def test_connect_mcp_retries_transient_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = FakeMCPTool(failures=2)
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("sundae_funday.ops_agent.asyncio.sleep", fake_sleep)

    await connect_mcp_with_retry(
        tool,
        attempts=4,
        initial_backoff_seconds=1,
    )

    assert tool.calls == [False, True, True]
    assert delays == [1, 2]


@pytest.mark.asyncio
async def test_connect_mcp_raises_after_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tool = FakeMCPTool(failures=3)

    async def fake_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("sundae_funday.ops_agent.asyncio.sleep", fake_sleep)

    with pytest.raises(ToolException, match="MCP server is not ready"):
        await connect_mcp_with_retry(
            tool,
            attempts=3,
            initial_backoff_seconds=1,
        )


def ops_settings(**kwargs) -> Settings:
    return Settings(
        openai_base_url="http://unused/v1",
        openai_chat_model="unused",
        openai_api_key="unused",
        **kwargs,
    )


@pytest.mark.parametrize(
    "values",
    [
        {"ops_demo_work_seconds": -1},
        {"ops_demo_work_seconds": 11},
        {"ops_demo_work_seconds": float("nan")},
        {"ops_demo_concurrency": 0},
        {"ops_demo_concurrency": 65},
    ],
)
def test_demo_capacity_settings_are_bounded(values: dict) -> None:
    with pytest.raises(ValidationError):
        ops_settings(**values)


@pytest.mark.asyncio
@pytest.mark.parametrize(("delay", "expected_peak"), [(0, 6), (0.001, 2)])
async def test_demo_capacity_is_opt_in_and_logs_timing(
    delay: float,
    expected_peak: int,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr("sundae_funday.ops_agent.tracer", provider.get_tracer("test"))
    executor = SundaeOpsExecutor(
        MagicMock(spec=Agent),
        MagicMock(spec=MCPStreamableHTTPTool),
        ops_settings(ops_demo_work_seconds=delay, ops_demo_concurrency=2),
    )
    active = peak = 0
    ready, release = asyncio.Event(), asyncio.Event()

    async def handle(_):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        if active == expected_peak:
            ready.set()
        await release.wait()
        active -= 1
        return ['{"can_make_now":true}']

    monkeypatch.setattr(executor, "_run_structured_request", handle)
    caplog.set_level(logging.INFO, logger="ops-agent")
    tasks = [
        asyncio.create_task(
            executor._run("test", AgentSession(), MagicMock(spec=TaskUpdater))
        )
        for _ in range(6)
    ]
    try:
        await asyncio.wait_for(ready.wait(), timeout=2)
    finally:
        release.set()
        await asyncio.gather(*tasks)
        provider.shutdown()

    assert peak == expected_peak
    logs = [
        record.message
        for record in caplog.records
        if "operation=scooper" in record.message
    ]
    assert len(logs) == 6
    assert all(
        "outcome=ok" in line and "pod=" in line and "trace_id=" in line for line in logs
    )
    spans = exporter.get_finished_spans()
    assert len(spans) == 6
    queue_times: list[float] = []
    for span in spans:
        assert span.attributes is not None
        assert span.attributes["ops.demo_work_seconds"] == delay
        assert span.attributes["gen_ai.agent.name"] == "Scooper"
        processing_ms = span.attributes["ops.processing_ms"]
        queue_ms = span.attributes["ops.queue_wait_ms"]
        assert isinstance(processing_ms, float)
        assert isinstance(queue_ms, float)
        assert processing_ms >= delay * 1000
        queue_times.append(queue_ms)
    if delay:
        assert max(queue_times) > 0


@pytest.mark.asyncio
async def test_demo_capacity_releases_slot_after_failure(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    executor = SundaeOpsExecutor(
        MagicMock(spec=Agent),
        MagicMock(spec=MCPStreamableHTTPTool),
        ops_settings(ops_demo_work_seconds=0.001, ops_demo_concurrency=1),
    )
    calls = 0

    async def handle(_):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("MCP unavailable")
        return ['{"can_make_now":true}']

    monkeypatch.setattr(executor, "_run_structured_request", handle)
    with pytest.raises(RuntimeError, match="MCP unavailable"):
        await executor._run("test", AgentSession(), MagicMock(spec=TaskUpdater))
    await asyncio.wait_for(
        executor._run("test", AgentSession(), MagicMock(spec=TaskUpdater)), timeout=2
    )
    assert "outcome=error" in caplog.text


def test_mcp_error_is_not_returned_as_success() -> None:
    with pytest.raises(RuntimeError, match="MCP tool failed"):
        parse_sundae_result(CallToolResult(isError=True, content=[]))
