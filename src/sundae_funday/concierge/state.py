"""Concierge session and tool-observation state."""

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from agent_framework import AgentResponse

from sundae_funday.concierge.api import RoutingPlan


@dataclass(slots=True)
class PendingDraft:
    draft_id: str
    idempotency_key: str
    quote: dict[str, Any]


@dataclass(slots=True)
class SessionState:
    history: list[tuple[str, str]] = field(default_factory=list)
    pending_draft: PendingDraft | None = None
    order_plan: RoutingPlan | None = None


@dataclass(slots=True)
class ToolObservation:
    name: str
    arguments: Any
    result: Any


class SessionStore:
    def __init__(self) -> None:
        self._lock = Lock()
        self._sessions: dict[str, SessionState] = {}

    def get(self, session_id: str) -> SessionState:
        with self._lock:
            return self._sessions.setdefault(session_id, SessionState())


def conversation_context(history: list[tuple[str, str]]) -> str:
    if not history:
        return "No prior conversation."
    lines: list[str] = []
    for user_message, reply in history[-6:]:
        lines.append(f"Customer: {user_message}")
        lines.append(f"Concierge: {reply}")
    return "\n".join(lines)


def extract_tool_observations(
    response: AgentResponse[Any],
) -> list[ToolObservation]:
    calls: dict[str, tuple[str, Any]] = {}
    observations: list[ToolObservation] = []
    for message in response.messages:
        for content in message.contents:
            if content.type == "function_call" and content.call_id:
                calls[content.call_id] = (
                    content.name or "unknown_tool",
                    content.arguments,
                )
            elif content.type == "function_result" and content.call_id:
                name, arguments = calls.get(content.call_id, ("unknown_tool", None))
                observations.append(
                    ToolObservation(
                        name=name,
                        arguments=arguments,
                        result=content.result,
                    )
                )
    return observations
