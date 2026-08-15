"""FastAPI concierge application."""

import contextlib
from importlib.resources import files
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from opentelemetry import trace

from sundae_funday.concierge.api import (
    ChatRequest,
    ChatResponse,
    ConfirmRequest,
    ConfirmResponse,
    Settings,
)
from sundae_funday.concierge.runtime import ConciergeRuntime
from sundae_funday.telemetry import configure, create_metrics_app, instrument_asgi

tracer = trace.get_tracer("sundae-funday.concierge")
INDEX_HTML = (
    files("sundae_funday.concierge").joinpath("static", "index.html").read_text()
)


async def root() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


def create_app(settings: Settings | None = None) -> Any:
    settings = settings or Settings()
    configure("concierge")
    runtime = ConciergeRuntime(settings)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(title="Sundae Funday Concierge", lifespan=lifespan)
    app.mount("/metrics", create_metrics_app())

    @app.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        return await root()

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": settings.app_version,
            "model_enabled": settings.model_is_enabled,
        }

    @app.post("/api/chat", response_model=ChatResponse)
    async def chat(request: ChatRequest) -> ChatResponse:
        with tracer.start_as_current_span("concierge.chat") as span:
            span.set_attribute("conversation.id", request.session_id)
            span.set_attribute("gen_ai.operation.name", "invoke_agent")
            span.set_attribute("gen_ai.agent.name", "SundaeConcierge")
            span.set_attribute("chat.message.length", len(request.message))
            try:
                response = await runtime.chat(request.session_id, request.message)
            except RuntimeError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error
            span.set_attribute("chat.route", response.source)
            span.set_attribute("chat.needs_confirmation", response.needs_confirmation)
            return response

    @app.post("/api/confirm", response_model=ConfirmResponse)
    async def confirm(request: ConfirmRequest) -> ConfirmResponse:
        with tracer.start_as_current_span("concierge.confirm") as span:
            span.set_attribute("conversation.id", request.session_id)
            try:
                return await runtime.confirm(request.session_id, request.customer_name)
            except RuntimeError as error:
                raise HTTPException(status_code=400, detail=str(error)) from error

    return instrument_asgi(app)
