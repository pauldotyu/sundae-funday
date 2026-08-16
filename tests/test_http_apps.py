import httpx
import pytest

from sundae_funday.concierge import Settings as ConciergeSettings
from sundae_funday.concierge import create_app as create_concierge_app
from sundae_funday.mcp_service import create_app as create_mcp_app
from sundae_funday.mcp_service import get_settings as get_mcp_settings
from sundae_funday.ops_agent import Settings as OpsSettings
from sundae_funday.ops_agent import create_agent_card


@pytest.mark.asyncio
async def test_concierge_http_routes_and_health_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENABLE_INSTRUMENTATION", "false")
    app = create_concierge_app(
        ConciergeSettings(
            app_version="9.8.7",
            openai_base_url="",
            openai_chat_model="",
        )
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        page = await client.get("/")
        health = await client.get("/healthz")
        invalid_chat = await client.post(
            "/api/chat",
            json={"session_id": "", "message": ""},
        )
        no_draft = await client.post(
            "/api/confirm",
            json={"session_id": "new-session"},
        )

    assert page.status_code == 200
    assert "<title>The Sundae Shop</title>" in page.text
    assert health.json() == {
        "status": "ok",
        "version": "9.8.7",
        "model_enabled": False,
    }
    assert invalid_chat.status_code == 422
    assert no_draft.status_code == 400
    assert no_draft.json() == {"detail": "There is no sundae waiting for confirmation"}


@pytest.mark.asyncio
async def test_mcp_health_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ENABLE_INSTRUMENTATION", "false")
    monkeypatch.setenv("APP_VERSION", "7.6.5")
    get_mcp_settings.cache_clear()
    app = create_mcp_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    ) as client:
        response = await client.get("/healthz")

    assert response.json() == {"status": "ok", "version": "7.6.5", "tools": 4}
    get_mcp_settings.cache_clear()


def test_ops_agent_card_contract() -> None:
    card = create_agent_card(
        OpsSettings(
            app_version="3.2.1",
            ops_agent_public_base_url="https://ops.example.test/base/",
            openai_base_url="https://models.example.test/v1",
            openai_chat_model="test-model",
            openai_api_key="test-key",
        )
    )

    assert card.name == "Scooper"
    assert card.version == "3.2.1"
    assert card.supported_interfaces[0].url == "https://ops.example.test/base/"
    assert card.supported_interfaces[0].protocol_binding == "JSONRPC"
    assert [skill.id for skill in card.skills] == ["support_sundae_operations"]
