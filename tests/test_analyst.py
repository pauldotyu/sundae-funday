import asyncio
import json
import subprocess
from collections.abc import Mapping
from datetime import date
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest

from sundae_funday.analyst import Settings, SprinklesExecutor, build_prompt
from sundae_funday.concierge import ConciergeRuntime, heuristic_plan
from sundae_funday.concierge import Settings as ConciergeSettings
from sundae_funday.sales import INJECTED_NOTE, seed_orders
from sundae_funday.sandbox import (
    CodeResult,
    ExecutorKind,
    InProcessExecutor,
    SandboxExecutor,
    format_probe,
    sandbox_command,
)
from sundae_funday.shop import InMemorySundaeShop

FILES = {"sales.json": json.dumps({"orders": [{"total_cents": 650}]})}
CODE = (
    "import json\n"
    "print(sum(o['total_cents'] for o in json.load(open('sales.json'))['orders']))"
)


def test_seeded_history_is_deterministic_with_trend_and_injection() -> None:
    today = date(2026, 11, 9)
    orders = seed_orders(today)

    assert orders == seed_orders(today)
    assert orders[-1]["notes"] == INJECTED_NOTE
    assert all(isinstance(order["total_cents"], int) for order in orders)
    by_day: dict[str, int] = {}
    for order in orders:
        by_day[order["placed_at"][:10]] = by_day.get(order["placed_at"][:10], 0) + 1
    weekend = [n for d, n in by_day.items() if date.fromisoformat(d).weekday() >= 4]
    weekday = [n for d, n in by_day.items() if date.fromisoformat(d).weekday() < 4]
    assert min(weekend) > max(weekday)
    half = len(orders) // 2
    mint = [
        sum(order["flavors"].count("MINT_CHIP") for order in part)
        for part in (orders[:half], orders[half:])
    ]
    assert mint[1] > mint[0]


def test_export_sales_is_read_only_and_includes_submitted_notes() -> None:
    shop = InMemorySundaeShop()
    before = shop.export_sales()
    quote = shop.quote_order(
        session_id="s1", flavors=["vanilla", "chocolate"], sauce="hot fudge"
    )
    assert shop.export_sales()["inventory"] == before["inventory"]

    shop.submit_order(
        draft_id=quote["draft_id"],
        session_id="s1",
        idempotency_key="k1",
        customer_name="Ada",
        notes="extra spoon",
    )

    after = shop.export_sales()
    assert len(after["orders"]) == len(before["orders"]) + 1
    assert after["orders"][-1]["notes"] == "extra spoon"
    assert after["orders"][-1]["total_cents"] == quote["quote"]["total_cents"]
    assert after["inventory"]["flavors"]["VANILLA"]["remaining"] == (
        before["inventory"]["flavors"]["VANILLA"]["remaining"] - 1
    )


def test_sandbox_command_round_trips_files(tmp_path: Any) -> None:
    files = {**FILES, "quote.txt": "it's $5 `now`"}
    code = f"{CODE}\nprint(open('quote.txt').read())"
    result = subprocess.run(
        ["/bin/sh", "-c", sandbox_command(code, files)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["650", "it's $5 `now`"]
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_inprocess_executor_runs_and_times_out() -> None:
    result = await InProcessExecutor(5).run(CODE, FILES)
    assert (result.stdout.strip(), result.exit_code) == ("650", 0)

    slow = await InProcessExecutor(0.2).run("import time\ntime.sleep(5)", {})
    assert slow.timed_out and slow.exit_code == 124


@pytest.mark.asyncio
async def test_sandbox_executor_posts_execute_and_maps_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != "/execute":
            return httpx.Response(503)
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"stdout": "650\n", "exit_code": 0})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs),
    )

    result = await SandboxExecutor("http://sandbox:8888/", 7).run(CODE, FILES)
    assert result == CodeResult("sandbox", "650\n", "", 0)
    assert seen[0]["timeout_seconds"] == 7
    assert "python3 main.py" in seen[0]["command"]

    with pytest.raises(RuntimeError, match="Sandbox runtime request failed"):
        await SandboxExecutor("http://sandbox:8888/down", 7).run(CODE, FILES)


class FakeAgent:
    def __init__(self, plans: list[dict[str, str]]) -> None:
        self.plans = plans
        self.prompts: list[str] = []

    async def run(self, prompt: str, **_: Any) -> Any:
        self.prompts.append(prompt)
        return SimpleNamespace(text=json.dumps(self.plans.pop(0)))


class FakeExecutor:
    kind = ExecutorKind.SANDBOX

    def __init__(self, results: list[CodeResult]) -> None:
        self.results = results

    async def run(self, code: str, files: Mapping[str, str]) -> CodeResult:
        assert "sales.json" in files
        return self.results.pop(0)


@pytest.mark.asyncio
async def test_analyst_repairs_failed_program_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sales = InMemorySundaeShop().export_sales()

    async def fake_mcp(*_: Any) -> object:
        return sales

    monkeypatch.setattr("sundae_funday.analyst.call_mcp_tool", fake_mcp)
    agent = FakeAgent(
        [
            {"rationale": "first", "code": "print(nope)"},
            {"rationale": "fixed", "code": "print('Mint Chip runs out Saturday')"},
        ]
    )
    executor = FakeExecutor(
        [
            CodeResult("sandbox", "", "NameError: nope", 1),
            CodeResult("sandbox", "Mint Chip runs out Saturday\n", "", 0),
        ]
    )
    settings = Settings(openai_base_url="http://model", openai_chat_model="m")
    sprinkles = SprinklesExecutor(cast(Any, agent), executor, settings)

    result = await sprinkles.analyze("What runs out before Saturday?")

    assert result["attempts"] == 2
    assert result["rationale"] == "fixed"
    assert result["exit_code"] == 0
    assert "NameError: nope" in agent.prompts[1]
    assert INJECTED_NOTE in build_prompt("q", sales)


@pytest.mark.asyncio
async def test_analyst_bounds_runaway_model_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_mcp(*_: Any) -> object:
        return {"orders": [], "inventory": {}}

    class HungAgent:
        async def run(self, *_: Any, **__: Any) -> Any:
            await asyncio.sleep(10)

    monkeypatch.setattr("sundae_funday.analyst.call_mcp_tool", fake_mcp)
    settings = Settings(
        openai_base_url="http://model",
        openai_chat_model="m",
        analyst_model_timeout_seconds=0.05,
    )
    sprinkles = SprinklesExecutor(cast(Any, HungAgent()), FakeExecutor([]), settings)

    with pytest.raises(RuntimeError, match="model call timed out"):
        await sprinkles.analyze("forecast")


def test_heuristic_routes_owner_questions_to_analytics() -> None:
    for message in (
        "Which flavors will run out before Saturday close, and what should I reorder?",
        "What were our best-selling toppings over the last two weeks?",
        "Summarize the special instructions customers left this week.",
    ):
        plan = heuristic_plan(message)
        assert plan.route == "analytics"
        assert plan.analytics_question == message
    assert heuristic_plan("Add rainbow sprinkles to a classic").route == "quote"


@pytest.mark.asyncio
async def test_concierge_analytics_route_renders_sandbox_result() -> None:
    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        raise AssertionError(name)

    async def fake_analyst(session_id: str, question: str) -> str:
        assert "best-selling" in question
        return json.dumps(
            {
                "code": "a\nb\nc",
                "executor": "sandbox",
                "exit_code": 0,
                "stdout": "Hot Fudge: 31",
            }
        )

    runtime = ConciergeRuntime(
        ConciergeSettings(openai_base_url="", openai_chat_model=""),
        mcp_call=fake_mcp,
        analyst_call=fake_analyst,
    )
    response = await runtime.chat("s1", "What are our best-selling toppings?")

    assert response.source == "analytics"
    assert "Hot Fudge: 31" in response.reply
    assert "3-line Python analysis in the sandbox executor" in response.reply


@pytest.mark.asyncio
async def test_concierge_analytics_disabled_without_url() -> None:
    async def fake_mcp(name: str, arguments: dict[str, object]) -> object:
        raise AssertionError(name)

    runtime = ConciergeRuntime(
        ConciergeSettings(openai_base_url="", openai_chat_model=""),
        mcp_call=fake_mcp,
    )
    response = await runtime.chat("s1", "Show me sales trends")

    assert response.source == "analytics"
    assert "not enabled" in response.reply


def test_format_probe_compares_executors_side_by_side() -> None:
    table = format_probe(
        {
            "inprocess": {
                "secret_env": ["OPENAI_API_KEY"],
                "network": {"1.1.1.1:443": "open"},
            },
            "sandbox": {"secret_env": [], "network": {"1.1.1.1:443": "blocked"}},
        }
    )
    lines = table.splitlines()
    assert lines[0].split() == ["check", "inprocess", "sandbox"]
    assert lines[1].split() == ["secret_env", "OPENAI_API_KEY", "none"]
    assert lines[2].split() == ["1.1.1.1:443", "open", "blocked"]
