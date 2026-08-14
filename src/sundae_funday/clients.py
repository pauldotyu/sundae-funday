"""Protocol helpers for MCP and A2A."""

import asyncio
import contextlib
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
) -> Any:
    normalized_url = f"{base_url.rstrip('/')}/"
    async with streamable_http_client(normalized_url) as streams:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
    return parse_mcp_result(result, name)


class OpsAgentClient:
    def __init__(self, base_url: str, timeout_seconds: float = 60) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._agent: A2AAgent | None = None
        self._sessions: dict[str, AgentSession] = {}
        self._exit_stack = contextlib.AsyncExitStack()
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        await self._exit_stack.aclose()

    async def _get_agent(self) -> A2AAgent:
        if self._agent is not None:
            return self._agent
        async with self._lock:
            if self._agent is not None:
                return self._agent
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                card = await A2ACardResolver(
                    httpx_client=client,
                    base_url=self.base_url,
                ).get_agent_card()
            agent = A2AAgent(
                name=card.name,
                agent_card=card,
                url=self.base_url,
                timeout=self.timeout_seconds,
            )
            self._agent = await self._exit_stack.enter_async_context(agent)
        return self._agent

    async def ask(self, session_id: str, question: str) -> str:
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
