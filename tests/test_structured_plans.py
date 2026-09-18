import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from agent_framework import (
    Agent,
    AgentResponse,
    Content,
    MCPStreamableHTTPTool,
    Message,
)
from agent_framework.exceptions import ChatClientException
from agent_framework.openai import OpenAIChatCompletionClient
from openai import AsyncOpenAI

from sundae_funday.agent_runtime import run_structured
from sundae_funday.concierge import ConciergeRuntime, RoutingPlan, Settings
from sundae_funday.model_client import OpenAIAuthMode, create_openai_chat_client
from sundae_funday.ops_agent import OperationsPlan, SundaeOpsExecutor, create_ops_agent
from sundae_funday.ops_agent import Settings as OpsSettings
from sundae_funday.shop import InMemorySundaeShop


def ops_settings() -> OpsSettings:
    return OpsSettings(
        openai_base_url="http://model/v1",
        openai_chat_model="test",
        openai_api_key="test",
    )


def response(text: str) -> AgentResponse:
    return AgentResponse(
        messages=[Message(role="assistant", contents=[Content.from_text(text)])]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["router", "ops"])
async def test_real_client_sends_json_schema_and_retries_validation_errors(
    target: str,
) -> None:
    requests: list[dict] = []
    expected = (
        RoutingPlan(route="operations", operations_intent="specials")
        if target == "router"
        else OperationsPlan(tool="check_availability")
    )

    def completion(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/chat/completions"
        body = json.loads(request.content)
        requests.append(body)
        schema = body["response_format"]
        assert schema["type"] == "json_schema"
        assert schema["json_schema"]["strict"] is True
        assert schema["json_schema"]["schema"]["additionalProperties"] is False
        assert not body.get("tools")
        text = expected.model_dump_json() if len(requests) == 2 else "{}"
        return httpx.Response(
            200,
            json={
                "id": "completion",
                "object": "chat.completion",
                "created": 0,
                "model": "test",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
            },
        )

    async with AsyncOpenAI(
        api_key="test",
        base_url="http://model/v1",
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(completion)),
    ) as sdk:
        model = OpenAIChatCompletionClient(async_client=sdk, model="test")
        if target == "router":
            runtime = ConciergeRuntime(
                Settings(openai_base_url="", openai_chat_model=""),
                ops_call=AsyncMock(),
            )
            runtime._router = runtime.create_router(model)
            result = await runtime.plan_turn(
                "Got any specials today?", "No prior conversation."
            )
        else:
            agent = create_ops_agent(ops_settings(), client=model)
            result = await run_structured(agent, "Check inventory", OperationsPlan)

    assert result == expected
    assert len(requests) == 2
    retry_prompt = requests[1]["messages"][-1]["content"]
    assert "Validation errors:" in retry_prompt
    assert '"type":"missing"' in retry_prompt


@pytest.mark.asyncio
async def test_factory_uses_chat_completions_for_openai_compatible_endpoints() -> None:
    client = create_openai_chat_client(
        model="test",
        base_url="http://model/v1",
        auth_mode=OpenAIAuthMode.API_KEY,
        api_key="test",
    )
    assert isinstance(client, OpenAIChatCompletionClient)
    await client.client.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "",
        "not JSON",
        '{"route":"operations","operations_intent":"unknown"}',
        '{"route":"quote","operations_intent":"specials"}',
        '{"route":"quote","flavors":42}',
        '{"route":"quote","flavors":[42]}',
        '{"route":"quote","requested_ready_in_minutes":true}',
        '{"route":"menu","unexpected":true}',
    ],
)
async def test_invalid_plans_retry_once_then_fall_back_with_a_warning(
    invalid: str, caplog: pytest.LogCaptureFixture
) -> None:
    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""), ops_call=AsyncMock()
    )
    router = MagicMock(spec=Agent)
    router.run = AsyncMock(return_value=response(invalid))
    runtime._router = router

    plan = await runtime.plan_turn("Any specials today?", "No prior conversation.")

    assert plan.operations_intent == "specials"
    assert router.run.await_count == 2
    assert "Router validation exhausted" in caplog.text
    assert "response_format" in router.run.call_args.kwargs["options"]


@pytest.mark.asyncio
async def test_specials_follow_model_intent_not_keywords() -> None:
    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""), ops_call=AsyncMock()
    )
    router = MagicMock(spec=Agent)
    router.run = AsyncMock(return_value=response('{"route":"general"}'))
    runtime._router = router

    plan = await runtime.plan_turn("No specials please", "No prior conversation.")

    assert plan.route == "general"
    assert router.run.await_count == 1


@pytest.mark.asyncio
async def test_specials_intent_executes_inventory_path_without_keyword_matching() -> (
    None
):
    ops_call = AsyncMock(
        return_value=json.dumps(InMemorySundaeShop().check_availability())
    )
    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""), ops_call=ops_call
    )
    router = MagicMock(spec=Agent)
    router.run = AsyncMock(
        return_value=response('{"route":"operations","operations_intent":"specials"}')
    )
    runtime._router = router

    reply = await runtime.chat("customer", "Recommend something you have plenty of")

    assert reply.source == "operations"
    assert "Today's special" in reply.reply
    assert '"operation":"inventory_special"' in ops_call.call_args.args[1]


@pytest.mark.asyncio
async def test_model_endpoint_errors_do_not_silently_fall_back() -> None:
    runtime = ConciergeRuntime(
        Settings(openai_base_url="", openai_chat_model=""), ops_call=AsyncMock()
    )
    router = MagicMock(spec=Agent)
    router.run = AsyncMock(side_effect=ChatClientException("Endpoint unavailable"))
    runtime._router = router

    with pytest.raises(RuntimeError, match="structured-output request failed"):
        await runtime.plan_turn("Any specials?", "No prior conversation.")

    assert router.run.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("list_menu", {}),
        ("check_availability", {"flavors": ["vanilla"], "sauce": None, "toppings": []}),
        (
            "quote_order",
            {
                "size": "CLASSIC",
                "flavors": ["vanilla"],
                "sauce": None,
                "toppings": [],
                "requested_ready_in_minutes": 10,
                "session_id": "ops-agent",
            },
        ),
    ],
)
async def test_ops_executes_only_the_validated_tool_plan(
    tool: str, arguments: dict
) -> None:
    agent = MagicMock(spec=Agent)
    agent.run = AsyncMock(
        return_value=response(
            json.dumps(
                {"tool": tool, "flavors": ["vanilla"], "requested_ready_in_minutes": 10}
            )
        )
    )
    tools = MagicMock(spec=MCPStreamableHTTPTool)
    tools.call_tool = AsyncMock(return_value='{"authoritative":true}')
    executor = SundaeOpsExecutor(agent, tools, ops_settings())

    result = await executor._run_model_request("Check this order")

    tools.call_tool.assert_awaited_once_with(tool, **arguments)
    assert result == ['{"authoritative":true}']


@pytest.mark.asyncio
async def test_ops_never_executes_an_invalid_or_submit_plan() -> None:
    agent = MagicMock(spec=Agent)
    agent.run = AsyncMock(return_value=response('{"tool":"submit_order"}'))
    tools = MagicMock(spec=MCPStreamableHTTPTool)
    executor = SundaeOpsExecutor(agent, tools, ops_settings())

    with pytest.raises(RuntimeError, match="valid operations plan"):
        await executor._run_model_request("Submit the order")

    assert agent.run.await_count == 2
    tools.call_tool.assert_not_called()
