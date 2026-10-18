from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from sundae_funday import telemetry

CONNECTION_STRING = (
    "InstrumentationKey=00000000-0000-0000-0000-000000000000;"
    "IngestionEndpoint=https://example.in.applicationinsights.azure.com/"
)


def test_connection_string_selects_azure_monitor(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", CONNECTION_STRING)
    spans, _, _ = telemetry._build_exporters()
    assert type(spans).__name__ == "AzureMonitorTraceExporter"


def test_platform_otlp_endpoint_wins_over_connection_string(monkeypatch):
    monkeypatch.setenv("APPLICATIONINSIGHTS_CONNECTION_STRING", CONNECTION_STRING)
    monkeypatch.setenv(
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://10.0.0.1:28331/v1/traces"
    )
    spans, _, _ = telemetry._build_exporters()
    assert isinstance(spans, OTLPSpanExporter)


def test_no_configuration_uses_otlp(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", raising=False)
    monkeypatch.delenv("APPLICATIONINSIGHTS_CONNECTION_STRING", raising=False)
    spans, _, _ = telemetry._build_exporters()
    assert isinstance(spans, OTLPSpanExporter)
