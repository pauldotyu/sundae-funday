"""Helpers for OpenAI compatible chat clients."""

from collections.abc import Sequence
from enum import StrEnum

from agent_framework import ChatAndFunctionMiddlewareTypes
from agent_framework.openai import OpenAIChatClient
from azure.identity import DefaultAzureCredential, WorkloadIdentityCredential


class OpenAIAuthMode(StrEnum):
    API_KEY = "api_key"
    DEFAULT_CREDENTIAL = "default_credential"
    WORKLOAD_IDENTITY = "workload_identity"


def model_enabled(base_url: str, model: str) -> bool:
    return bool(base_url.strip() and model.strip())


def validate_openai_auth(
    auth_mode: OpenAIAuthMode,
    api_key: str | None,
) -> None:
    if auth_mode is OpenAIAuthMode.API_KEY and not (api_key or "").strip():
        raise ValueError("OPENAI_API_KEY must be set when OPENAI_AUTH_MODE=api_key")


def create_openai_chat_client(
    *,
    model: str,
    base_url: str,
    auth_mode: OpenAIAuthMode,
    api_key: str | None = None,
    middleware: Sequence[ChatAndFunctionMiddlewareTypes] | None = None,
) -> OpenAIChatClient:
    model = model.strip()
    base_url = base_url.strip()
    if not model:
        raise ValueError("OPENAI_CHAT_MODEL must not be empty")
    if not base_url:
        raise ValueError("OPENAI_BASE_URL must not be empty")

    validate_openai_auth(auth_mode, api_key)

    if auth_mode is OpenAIAuthMode.API_KEY:
        return OpenAIChatClient(
            model=model,
            api_key=api_key,
            base_url=base_url,
            middleware=middleware,
        )
    if auth_mode is OpenAIAuthMode.DEFAULT_CREDENTIAL:
        return OpenAIChatClient(
            model=model,
            credential=DefaultAzureCredential(),
            base_url=base_url,
            middleware=middleware,
        )
    if auth_mode is OpenAIAuthMode.WORKLOAD_IDENTITY:
        return OpenAIChatClient(
            model=model,
            credential=WorkloadIdentityCredential(),
            base_url=base_url,
            middleware=middleware,
        )
    raise ValueError(f"Unsupported OPENAI_AUTH_MODE: {auth_mode}")
