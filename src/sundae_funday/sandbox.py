"""Code execution boundaries for agent-generated Python."""

import asyncio
import base64
import hashlib
import json
import shlex
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

import httpx
from opentelemetry import trace
from pydantic import Field

from sundae_funday.settings import AppSettings
from sundae_funday.telemetry import inject_trace_headers

tracer = trace.get_tracer("sundae-funday.sandbox")
OUTPUT_LIMIT = 4000


class ExecutorKind(StrEnum):
    SANDBOX = "sandbox"
    INPROCESS = "inprocess"


@dataclass(frozen=True, slots=True)
class CodeResult:
    executor: str
    stdout: str
    stderr: str
    exit_code: int
    timed_out: bool = False


class CodeExecutor(Protocol):
    kind: ExecutorKind

    async def run(self, code: str, files: Mapping[str, str]) -> CodeResult: ...


def _result(
    kind: ExecutorKind,
    stdout: str,
    stderr: str,
    exit_code: int,
    timed_out: bool = False,
) -> CodeResult:
    return CodeResult(
        kind.value,
        stdout[-OUTPUT_LIMIT:],
        stderr[-OUTPUT_LIMIT:],
        exit_code,
        timed_out,
    )


class InProcessExecutor:
    """Runs code in the agent's own container, with the agent's env and network.

    This is the unsafe baseline the sandbox exists to replace.
    """

    kind = ExecutorKind.INPROCESS

    def __init__(self, timeout_seconds: float) -> None:
        self.timeout_seconds = timeout_seconds

    async def run(self, code: str, files: Mapping[str, str]) -> CodeResult:
        with tempfile.TemporaryDirectory() as workdir:
            for name, content in {**files, "main.py": code}.items():
                Path(workdir, name).write_text(content)
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "main.py",
                cwd=workdir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(), self.timeout_seconds
                )
            except TimeoutError:
                process.kill()
                await process.wait()
                return _result(self.kind, "", "timed out", 124, timed_out=True)
            return _result(
                self.kind,
                stdout.decode(errors="replace"),
                stderr.decode(errors="replace"),
                process.returncode or 0,
            )


def sandbox_command(code: str, files: Mapping[str, str]) -> str:
    writes = " && ".join(
        f"printf %s {shlex.quote(base64.b64encode(content.encode()).decode())}"
        f" | base64 -d > {shlex.quote(name)}"
        for name, content in {**files, "main.py": code}.items()
    )
    return (
        f'd=$(mktemp -d) && cd "$d" && {writes} && python3 main.py; '
        'status=$?; cd / && rm -rf "$d"; exit $status'
    )


class SandboxExecutor:
    """Runs code through the Agent Sandbox python runtime `/execute` API."""

    kind = ExecutorKind.SANDBOX

    def __init__(self, base_url: str, timeout_seconds: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    async def run(self, code: str, files: Mapping[str, str]) -> CodeResult:
        try:
            async with httpx.AsyncClient(
                headers=inject_trace_headers(),
                timeout=self.timeout_seconds + 10,
            ) as client:
                response = await client.post(
                    f"{self.base_url}/execute",
                    json={
                        "command": sandbox_command(code, files),
                        "timeout_seconds": self.timeout_seconds,
                    },
                )
                response.raise_for_status()
        except httpx.HTTPError as error:
            raise RuntimeError("Sandbox runtime request failed") from error
        data = response.json()
        return _result(
            self.kind,
            str(data.get("stdout", "")),
            str(data.get("stderr", "")),
            int(data.get("exit_code", 1)),
            bool(data.get("timed_out", False)),
        )


async def execute(
    executor: CodeExecutor,
    code: str,
    files: Mapping[str, str],
) -> CodeResult:
    with tracer.start_as_current_span("sandbox.execute") as span:
        span.set_attribute("sandbox.executor", executor.kind.value)
        span.set_attribute("code.sha256", hashlib.sha256(code.encode()).hexdigest())
        span.set_attribute("code.lines", code.count("\n") + 1)
        result = await executor.run(code, files)
        span.set_attribute("process.exit_code", result.exit_code)
        span.set_attribute("sandbox.timed_out", result.timed_out)
        return result


# What generated code could do if a prompt injection steered it. Names only,
# never values.
PROBE_CODE = """
import json, os, platform, socket
HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "CONNECTION_STRING", "CLIENT_ID")
FILES = (
    "/var/run/secrets/kubernetes.io/serviceaccount/token",
    os.environ.get("AZURE_FEDERATED_TOKEN_FILE", ""),
    os.path.expanduser("~/.azure"),
)
def reach(host, port):
    try:
        socket.create_connection((host, port), timeout=2).close()
        return "reachable"
    except OSError as error:
        return f"blocked ({type(error).__name__})"
targets = json.load(open("targets.json"))
print(json.dumps({
    "kernel": platform.release(),
    "uid": os.getuid(),
    "secret_env": sorted(
        k for k, v in os.environ.items()
        if v and k != "GPG_KEY" and any(h in k for h in HINTS)
    ),
    "credential_files": [p for p in FILES if p and os.access(p, os.R_OK)],
    "network": {f"{h}:{p}": reach(h, p) for h, p in targets},
}))
""".strip()


class ProbeSettings(AppSettings):
    sandbox_url: str = "http://sandbox:8888"
    sandbox_timeout_seconds: float = Field(default=20, gt=0, le=120)
    sundae_mcp_url: str = "http://sundae-mcp:8101/mcp/"
    openai_base_url: str = ""


def probe_targets(settings: ProbeSettings) -> list[tuple[str, int]]:
    targets = [("1.1.1.1", 443)]
    for url in (settings.sundae_mcp_url, settings.openai_base_url):
        parts = urlsplit(url)
        if parts.hostname:
            port = parts.port or (443 if parts.scheme == "https" else 80)
            targets.append((parts.hostname, port))
    return targets


async def probe(
    executors: list[CodeExecutor],
    targets: list[tuple[str, int]],
) -> dict[str, dict[str, object]]:
    report: dict[str, dict[str, object]] = {}
    files = {"targets.json": json.dumps(targets)}
    for executor in executors:
        result = await execute(executor, PROBE_CODE, files)
        try:
            report[executor.kind.value] = json.loads(result.stdout)
        except json.JSONDecodeError:
            report[executor.kind.value] = {"error": asdict(result)}
    return report


def format_probe(report: dict[str, dict[str, object]]) -> str:
    columns = list(report)
    keys: list[str] = []
    for findings in report.values():
        for key, value in findings.items():
            names = (
                list(value) if key == "network" and isinstance(value, dict) else [key]
            )
            keys.extend(name for name in names if name not in keys)

    def cell(findings: dict[str, object], key: str) -> str:
        network = findings.get("network")
        value = (
            network.get(key)
            if isinstance(network, dict) and key in network
            else findings.get(key)
        )
        if isinstance(value, list):
            return ", ".join(map(str, value)) or "none"
        return "n/a" if value is None else str(value)

    rows = [["check", *columns]]
    rows += [[key, *(cell(report[c], key) for c in columns)] for key in keys]
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join(
        "  ".join(value.ljust(widths[i]) for i, value in enumerate(row)).rstrip()
        for row in rows
    )


def main() -> int:
    settings = ProbeSettings()
    executors: list[CodeExecutor] = [
        InProcessExecutor(settings.sandbox_timeout_seconds),
        SandboxExecutor(settings.sandbox_url, settings.sandbox_timeout_seconds),
    ]
    report = asyncio.run(probe(executors, probe_targets(settings)))
    print(format_probe(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
