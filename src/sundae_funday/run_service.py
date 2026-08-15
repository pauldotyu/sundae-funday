"""Run one of the app services with uvicorn."""

import os
import sys
from dataclasses import dataclass

import uvicorn

from sundae_funday.types import ServiceName


@dataclass(frozen=True, slots=True)
class Service:
    app: str
    port: int


SERVICES = {
    ServiceName.CONCIERGE: Service("sundae_funday.concierge:create_app", 8301),
    ServiceName.OPS_AGENT: Service("sundae_funday.ops_agent:create_app", 8202),
    ServiceName.SUNDAE_MCP: Service("sundae_funday.mcp_service:create_app", 8101),
}


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    raw_service = args[0] if args else os.getenv("SERVICE", ServiceName.CONCIERGE)
    try:
        service_name = ServiceName(raw_service)
    except ValueError as error:
        raise SystemExit(f"Unknown SERVICE value: {raw_service}") from error
    service = SERVICES[service_name]
    uvicorn.run(
        service.app,
        factory=True,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", str(service.port))),
        log_level=os.getenv("UVICORN_LOG_LEVEL", "info"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
