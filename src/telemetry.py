"""OpenTelemetry setup for local OTLP and Azure Monitor."""

import logging
import os
from importlib.util import find_spec
from threading import Lock
from typing import Any

from opentelemetry import metrics, trace
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.propagate import inject
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import make_asgi_app

_configure_lock = Lock()
_otel_configured = False


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def inject_trace_headers(_: dict[str, Any] | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    inject(headers)
    return headers


def _build_exporters() -> tuple[Any, Any, Any]:
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if connection_string:
        from azure.monitor.opentelemetry.exporter import (
            AzureMonitorLogExporter,
            AzureMonitorMetricExporter,
            AzureMonitorTraceExporter,
        )

        return (
            AzureMonitorTraceExporter(connection_string=connection_string),
            AzureMonitorMetricExporter(connection_string=connection_string),
            AzureMonitorLogExporter(connection_string=connection_string),
        )
    return OTLPSpanExporter(), OTLPMetricExporter(), OTLPLogExporter()


def uninstrument_httpx_client(client: Any) -> None:
    if not env_bool("ENABLE_INSTRUMENTATION", True):
        return
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

    HTTPXClientInstrumentor.uninstrument_client(client)


def configure(service_name: str) -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if not env_bool("ENABLE_INSTRUMENTATION", True):
        return

    global _otel_configured
    with _configure_lock:
        if _otel_configured:
            return
        attributes = {"service.name": service_name}
        namespace = os.getenv("OTEL_SERVICE_NAMESPACE", "").strip()
        if namespace:
            attributes["service.namespace"] = namespace
        resource = Resource.create(attributes)
        span_exporter, metric_exporter, log_exporter = _build_exporters()

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
        trace.set_tracer_provider(tracer_provider)

        metric_reader = PeriodicExportingMetricReader(metric_exporter)
        metrics.set_meter_provider(
            MeterProvider(resource=resource, metric_readers=[metric_reader])
        )

        logger_provider = LoggerProvider(resource=resource)
        logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
        set_logger_provider(logger_provider)
        logging.getLogger().addHandler(
            LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
        )

        if find_spec("httpx") is not None:
            from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

            HTTPXClientInstrumentor().instrument()
        _otel_configured = True


def create_metrics_app() -> Any:
    return make_asgi_app()


def instrument_asgi(app: Any) -> Any:
    if not env_bool("ENABLE_INSTRUMENTATION", True):
        return app
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    return OpenTelemetryMiddleware(
        app,
        excluded_urls=r".*/healthz$|.*/metrics/?$",
        exclude_spans=["receive", "send"],
    )
