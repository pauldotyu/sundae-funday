"""MCP and A2A protocol helpers."""

import asyncio
import json
from collections.abc import Mapping
from typing import Any, Protocol

import httpx
from a2a.client import A2ACardResolver
from a2a.types import TaskState
from agent_framework import AgentResponse, AgentSession
from agent_framework.a2a import A2AAgent
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from opentelemetry import trace

from sundae_funday.settings import normalize_url
from sundae_funday.telemetry import inject_trace_headers

tracer = trace.get_tracer("sundae-funday.clients")


class MCPResult(Protocol):
    isError: bool
    structuredContent: dict[str, Any] | None
    content: list[Any]


def result_text(result: MCPResult, *, sort_keys: bool = False) -> str:
    if result.structuredContent is not None:
        return json.dumps(
            result.structuredContent,
            separators=(",", ":"),
            sort_keys=sort_keys,
        )
    for content in result.content:
        text = getattr(content, "text", None)
        if text:
            return str(text)
    raise RuntimeError("Sundae MCP returned no result")


def parse_mcp_result(result: MCPResult, name: str) -> Any:
    if result.isError:
        raise RuntimeError(f"Sundae MCP tool failed: {name}")
    if result.structuredContent is not None:
        if set(result.structuredContent) == {"result"}:
            return result.structuredContent["result"]
        return result.structuredContent
    return json.loads(result_text(result))


async def call_mcp_tool(
    base_url: str,
    name: str,
    arguments: dict[str, Any],
    timeout_seconds: float = 60,
) -> Any:
    with tracer.start_as_current_span("mcp.client.call_tool") as span:
        span.set_attribute("rpc.system", "mcp")
        span.set_attribute("rpc.method", "tools/call")
        span.set_attribute("gen_ai.tool.name", name)
        async with httpx.AsyncClient(
            headers=inject_trace_headers(),
            timeout=timeout_seconds,
        ) as http_client:
            async with streamable_http_client(
                normalize_url(base_url),
                http_client=http_client,
            ) as streams:
                read, write = streams[0], streams[1]
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments)
    return parse_mcp_result(result, name)


class OpsAgentClient:
    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 60,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._http_client = httpx.AsyncClient(
            timeout=timeout_seconds,
            event_hooks={"request": [self._inject_trace_headers]},
        )
        self._agent: A2AAgent | None = None
        self._sessions: dict[str, AgentSession] = {}
        self._lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()

    async def _inject_trace_headers(self, request: httpx.Request) -> None:
        request.headers.update(inject_trace_headers())

    async def close(self) -> None:
        if not self._http_client.is_closed:
            await self._http_client.aclose()

    async def _get_agent(self) -> A2AAgent:
        if self._agent is not None:
            return self._agent
        async with self._lock:
            if self._agent is not None:
                return self._agent
            card = await A2ACardResolver(
                httpx_client=self._http_client,
                base_url=self.base_url,
            ).get_agent_card()
            self._agent = A2AAgent(
                name=card.name,
                agent_card=card,
                url=self.base_url,
                http_client=self._http_client,
                timeout=self.timeout_seconds,
            )
        return self._agent

    async def ask(self, session_id: str, question: str) -> str:
        async with self._call_lock:
            agent = await self._get_agent()
            session = self._sessions.setdefault(
                session_id,
                agent.create_session(session_id=f"{session_id}:ops"),
            )
            response = await agent.run(question, session=session)
            service_session = session.service_session_id
            task_state = (
                service_session.get("task_state")
                if isinstance(service_session, Mapping)
                else None
            )
            if task_state in {
                TaskState.TASK_STATE_FAILED,
                TaskState.TASK_STATE_CANCELED,
                TaskState.TASK_STATE_REJECTED,
            }:
                raise RuntimeError("Operations agent failed to complete the request")
            if not response.text:
                raise RuntimeError("Operations agent returned no response")
            return response.text


def extract_function_results(response: AgentResponse[Any]) -> list[str]:
    results: list[str] = []
    for message in response.messages:
        for content in message.contents:
            if content.type != "function_result":
                continue
            result = content.result
            if isinstance(result, str):
                results.append(result)
            elif result is not None:
                results.append(json.dumps(result, separators=(",", ":")))
    return results


def extract_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise RuntimeError("No JSON object found")
