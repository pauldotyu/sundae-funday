"""Concierge settings and external API models."""

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sundae_funday.model_client import (
    OpenAIAuthMode,
    model_enabled,
    validate_openai_auth,
)
from sundae_funday.settings import AppSettings, normalize_url


class Settings(AppSettings):
    sundae_mcp_url: str = "http://sundae-mcp:8101/mcp/"
    ops_agent_url: str = "http://ops-agent:8202"
    openai_base_url: str = ""
    openai_chat_model: str = ""
    openai_auth_mode: OpenAIAuthMode = OpenAIAuthMode.API_KEY
    openai_api_key: str | None = None
    agent_http_timeout_seconds: float = 60

    @property
    def normalized_sundae_mcp_url(self) -> str:
        return normalize_url(self.sundae_mcp_url)

    @property
    def model_is_enabled(self) -> bool:
        return model_enabled(self.openai_base_url, self.openai_chat_model)

    @model_validator(mode="after")
    def require_auth_if_enabled(self) -> Self:
        if self.model_is_enabled:
            validate_openai_auth(self.openai_auth_mode, self.openai_api_key)
        return self


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1)
    message: str = Field(min_length=1)


class ConfirmRequest(BaseModel):
    session_id: str = Field(min_length=1)
    customer_name: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    needs_confirmation: bool = False
    draft_id: str | None = None
    source: Literal["menu", "quote", "operations", "surprise", "general"]


class ConfirmResponse(BaseModel):
    session_id: str
    reply: str
    order: dict[str, Any]


class RoutingPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    route: Literal["menu", "quote", "operations", "surprise", "general"]
    operations_intent: Literal["specials", "availability"] = "availability"
    size: str | None = None
    flavors: list[str] = Field(default_factory=list)
    sauce: str | None = None
    toppings: list[str] = Field(default_factory=list)
    requested_ready_in_minutes: int | None = Field(default=None, ge=1)
    operations_question: str | None = None

    @model_validator(mode="after")
    def validate_operations_intent(self) -> Self:
        if self.operations_intent == "specials" and self.route != "operations":
            raise ValueError("specials requires route=operations")
        return self
