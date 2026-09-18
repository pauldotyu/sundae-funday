import asyncio
import json

import httpx
import pytest

from scripts.load import run_load


@pytest.mark.asyncio
@pytest.mark.parametrize("failures", [False, True])
async def test_load_is_bounded_read_only_and_reports_failures(
    failures: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    sessions: set[str] = set()
    active = peak = 0

    async def respond(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        assert request.url.path == "/api/chat"
        payload = json.loads(request.content)
        assert payload["message"] == "Got any specials today?"
        assert payload["session_id"] not in sessions
        sessions.add(payload["session_id"])
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.001)
        active -= 1
        return httpx.Response(
            200,
            json={
                "session_id": payload["session_id"],
                "reply": "Special",
                "source": "general" if failures else "operations",
            },
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://test"
    ) as client:
        report = await run_load(client, requests=10, concurrency=3)

    assert peak == 3
    assert len(sessions) == 10
    assert report["failed"] == (10 if failures else 0)
    assert report["succeeded"] == (0 if failures else 10)
    assert (report["p95_ms"] is None) == failures
    assert bool(capsys.readouterr().err) == failures


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 500])
async def test_http_errors_count_as_load_failures(status: int) -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(status)),
        base_url="http://test",
    ) as client:
        report = await run_load(client, requests=2, concurrency=1)

    assert report["failed"] == 2
    assert report["successful_requests_per_second"] == 0
