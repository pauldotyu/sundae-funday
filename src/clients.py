"""MCP and A2A protocol helpers."""

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import httpx
from a2a.client import A2ACardResolver
from a2a.types import TaskState
from agent_framework import AgentSession
from agent_framework.a2a import A2AAgent
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from opentelemetry import trace

from telemetry import inject_trace_headers

tracer = trace.get_tracer("sundae-funday.clients")


def parse_mcp_result(result: Any, name: str) -> Any:
    if result.isError:
        raise RuntimeError(f"Sundae MCP tool failed: {name}")
    if result.structuredContent is not None:
        if set(result.structuredContent) == {"result"}:
            return result.structuredContent["result"]
        return result.structuredContent
    for content in result.content:
        text = getattr(content, "text", None)
        if text:
            return json.loads(text)
    raise RuntimeError(f"Sundae MCP tool returned no JSON: {name}")


async def call_mcp_tool(
    base_url: str,
    name: str,
    arguments: dict[str, Any],
    timeout_seconds: float = 60,
) -> Any:
    normalized_url = f"{base_url.rstrip('/')}/"

    with tracer.start_as_current_span("mcp.client.call_tool") as span:
        span.set_attribute("rpc.system", "mcp")
        span.set_attribute("rpc.method", "tools/call")
        span.set_attribute("gen_ai.tool.name", name)
        async with httpx.AsyncClient(
            headers=inject_trace_headers(),
            timeout=timeout_seconds,
        ) as http_client:
            async with streamable_http_client(
                normalized_url,
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
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client or httpx.AsyncClient(timeout=timeout_seconds)
        self._agent: A2AAgent | None = None
        self._sessions: dict[str, AgentSession] = {}
        self._lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()
        self._active_trace_headers: dict[str, str] | None = None
        self._http_client.event_hooks["request"].append(self._inject_trace_headers)

    async def _inject_trace_headers(self, request: httpx.Request) -> None:
        if self._active_trace_headers is not None:
            request.headers.update(self._active_trace_headers)

    async def close(self) -> None:
        if self._http_client is not None and not self._http_client.is_closed:
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
            agent = A2AAgent(
                name=card.name,
                agent_card=card,
                url=self.base_url,
                http_client=self._http_client,
                timeout=self.timeout_seconds,
            )
            self._agent = agent
        return self._agent

    async def ask(self, session_id: str, question: str) -> str:
        async with self._call_lock:
            self._active_trace_headers = inject_trace_headers()
            try:
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
                    raise RuntimeError(
                        "Operations agent failed to complete the request"
                    )
                if not response.text:
                    raise RuntimeError("Operations agent returned no response")
                return response.text
            finally:
                self._active_trace_headers = None


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
