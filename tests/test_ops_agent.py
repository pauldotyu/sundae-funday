import pytest
from agent_framework.exceptions import ToolException

from ops_agent import connect_mcp_with_retry


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

    monkeypatch.setattr("ops_agent.asyncio.sleep", fake_sleep)

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

    monkeypatch.setattr("ops_agent.asyncio.sleep", fake_sleep)

    with pytest.raises(ToolException, match="MCP server is not ready"):
        await connect_mcp_with_retry(
            tool,
            attempts=3,
            initial_backoff_seconds=1,
        )
