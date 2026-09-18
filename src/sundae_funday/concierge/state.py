"""Concierge conversation and pending-order state."""

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

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
