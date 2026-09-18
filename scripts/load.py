"""Bounded, read-only customer load for the Scooper scaling demo."""

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
import uuid

import httpx

from sundae_funday.concierge.api import ChatResponse


async def run_load(
    client: httpx.AsyncClient, *, requests: int, concurrency: int
) -> dict[str, int | float | None]:
    if requests < 1 or concurrency < 1:
        raise ValueError("requests and concurrency must be positive")
    pending = iter(range(requests))
    run_id = uuid.uuid4().hex
    latencies: list[float] = []
    failures = 0

    async def worker() -> None:
        nonlocal failures
        for index in pending:
            session_id = f"load-{run_id}-{index}"
            started = time.perf_counter()
            try:
                response = await client.post(
                    "/api/chat",
                    json={
                        "session_id": session_id,
                        "message": "Got any specials today?",
                    },
                )
                response.raise_for_status()
                result = ChatResponse.model_validate(response.json())
                if (
                    result.session_id != session_id
                    or result.source != "operations"
                    or not result.reply.strip()
                    or result.needs_confirmation
                ):
                    raise ValueError("Expected an operations reply without an order")
            except (httpx.HTTPError, ValueError) as error:
                failures += 1
                print(f"request={index} failed: {error}", file=sys.stderr)
            else:
                latencies.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    await asyncio.gather(*(worker() for _ in range(min(requests, concurrency))))
    elapsed = time.perf_counter() - started
    latencies.sort()
    return {
        "requests": requests,
        "concurrency": concurrency,
        "succeeded": len(latencies),
        "failed": failures,
        "elapsed_seconds": round(elapsed, 2),
        "successful_requests_per_second": round(len(latencies) / elapsed, 2),
        "p50_ms": round(statistics.median(latencies), 1) if latencies else None,
        "p95_ms": (
            round(latencies[math.ceil(len(latencies) * 0.95) - 1], 1)
            if latencies
            else None
        ),
    }


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8301")
    parser.add_argument("--requests", type=positive_int, default=120)
    parser.add_argument("--concurrency", type=positive_int, default=12)
    args = parser.parse_args()
    async with httpx.AsyncClient(base_url=args.url, timeout=60) as client:
        report = await run_load(
            client, requests=args.requests, concurrency=args.concurrency
        )
    print(json.dumps(report, indent=2))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
