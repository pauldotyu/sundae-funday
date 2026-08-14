"""Run one of the app services with uvicorn."""

import os
import sys

import uvicorn

SERVICE_APPS = {
    "concierge": "sundae_funday.concierge:create_app",
    "ops-agent": "sundae_funday.ops_agent:create_app",
    "sundae-mcp": "sundae_funday.mcp_service:create_app",
}
SERVICE_PORTS = {
    "concierge": 8301,
    "ops-agent": 8202,
    "sundae-mcp": 8101,
}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    service = args[0] if args else os.getenv("SERVICE", "concierge")
    if service not in SERVICE_APPS:
        raise SystemExit(f"Unknown SERVICE value: {service}")
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", str(SERVICE_PORTS[service])))
    log_level = os.getenv("UVICORN_LOG_LEVEL", "info")
    uvicorn.run(
        SERVICE_APPS[service],
        factory=True,
        host=host,
        port=port,
        log_level=log_level,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
