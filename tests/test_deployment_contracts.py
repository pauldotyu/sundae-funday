import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
CHART = ROOT / "deploy/helm/sundae-funday"


def require_command(name: str) -> None:
    if shutil.which(name) is None:
        pytest.skip(f"{name} is not installed")


def rendered_documents(values: str | None = None) -> dict[tuple[str, str], str]:
    require_command("helm")
    command = [
        "helm",
        "template",
        "sundae-funday",
        str(CHART),
        "--namespace",
        "demo",
    ]
    if values is not None:
        command.extend(["--values", str(ROOT / values)])
    output = subprocess.run(
        command,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    documents: dict[tuple[str, str], str] = {}
    for document in output.split("\n---\n"):
        lines = document.splitlines()
        kind = next(
            (
                line.removeprefix("kind: ")
                for line in lines
                if line.startswith("kind: ")
            ),
            None,
        )
        metadata_index = next(
            (index for index, line in enumerate(lines) if line == "metadata:"),
            None,
        )
        if kind is None or metadata_index is None:
            continue
        name = next(
            (
                line.strip().removeprefix("name: ")
                for line in lines[metadata_index + 1 :]
                if line.startswith("  name: ")
            ),
            None,
        )
        if name is not None:
            documents[(kind, name)] = "\n".join(line.rstrip() for line in lines)
    return documents


def test_compose_application_contract() -> None:
    require_command("docker")
    result = subprocess.run(
        ["docker", "compose", "config", "--format", "json"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "IMAGE_TAG": "test"},
    )
    services = json.loads(result.stdout)["services"]

    expected = {
        "sundae-mcp": 8101,
        "ops-agent": 8202,
        "concierge": 8301,
    }
    for name, port in expected.items():
        service = services[name]
        assert service["image"] == "sundae-funday:test"
        assert service["environment"]["SERVICE"] == name
        assert int(service["environment"]["PORT"]) == port
        assert service["extra_hosts"] == ["host.docker.internal=host-gateway"]
        assert service["healthcheck"]["test"] == [
            "CMD",
            "sundae-funday-healthcheck",
        ]
        assert service["restart"] == "unless-stopped"


@pytest.mark.parametrize(
    ("values", "image"),
    [
        (None, "ghcr.io/pauldotyu/sundae-funday:0.1.0"),
        ("deploy/helm/values-local.yaml", "sundae-funday:0.1.0"),
        (
            "deploy/helm/values-azure.yaml",
            "ghcr.io/pauldotyu/sundae-funday:0.1.0",
        ),
    ],
)
def test_helm_workload_contract(values: str | None, image: str) -> None:
    documents = rendered_documents(values)
    expected = {
        "sundae-mcp": (8101, "cpu: 100m", "memory: 128Mi"),
        "ops-agent": (8202, "cpu: 200m", "memory: 256Mi"),
        "concierge": (8301, "cpu: 200m", "memory: 256Mi"),
    }

    for name, (port, cpu, memory) in expected.items():
        deployment = documents[("Deployment", name)]
        service = documents[("Service", name)]
        assert f'image: "{image}"' in deployment
        assert f"value: {name}" in deployment
        assert f'value: "{port}"' in deployment
        assert deployment.count("path: /healthz") == 3
        assert 'prometheus.io/scrape: "true"' in deployment
        assert cpu in deployment
        assert memory in deployment
        assert "runAsNonRoot: true" in deployment
        assert "readOnlyRootFilesystem: true" in deployment
        assert f"containerPort: {port}" in deployment
        assert "targetPort: http" in service
        assert "initContainers:" not in deployment


def test_helm_profile_specific_contracts() -> None:
    local = rendered_documents("deploy/helm/values-local.yaml")
    azure = rendered_documents("deploy/helm/values-azure.yaml")

    assert "nodePort: 30001" in local[("Service", "concierge")]
    for name in ("sundae-mcp", "ops-agent", "concierge"):
        assert "hostNetwork: true" in local[("Deployment", name)]
        assert "type: Recreate" in local[("Deployment", name)]
    local_config = local[("ConfigMap", "app-config")]
    assert 'SUNDAE_MCP_URL: "http://127.0.0.1:8101/mcp/"' in local_config
    assert 'OPS_AGENT_URL: "http://127.0.0.1:8202"' in local_config
    assert 'azure.workload.identity/use: "true"' in azure[("Deployment", "concierge")]
    assert 'azure.workload.identity/use: "true"' in azure[("Deployment", "ops-agent")]
    assert "serviceAccountName: demo" in azure[("Deployment", "concierge")]
