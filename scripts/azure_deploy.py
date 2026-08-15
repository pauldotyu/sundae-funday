#!/usr/bin/env python3
"""Render or deploy Sundae Funday to AKS from Terraform outputs."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "deploy/helm/sundae-funday"
AZURE_VALUES = ROOT / "deploy/helm/values-azure.yaml"
DEFAULT_LOCATION_KEY = "West US 3"


def run(
    command: Sequence[str],
    *,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=capture_output,
    )


class TerraformOutputs:
    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = values

    def output(self, name: str) -> Any:
        if name not in self._values:
            raise ValueError(f"Terraform output is missing {name!r}")
        entry = self._values[name]
        if isinstance(entry, dict) and "value" in entry:
            return entry["value"]
        return entry

    def regional(self, name: str, location_key: str) -> str:
        value = self.output(name)
        if not isinstance(value, dict):
            return str(value)
        if location_key in value:
            return str(value[location_key])
        if len(value) == 1:
            return str(next(iter(value.values())))
        available = ", ".join(sorted(str(key) for key in value))
        raise ValueError(
            f"Terraform output {name!r} has multiple regions. "
            f"Set AZURE_LOCATION_KEY to one of: {available}"
        )


@dataclass(frozen=True, slots=True)
class AzureContext:
    subscription_id: str
    resource_group: str
    cluster_name: str
    identity_resource_group: str
    identity_name: str
    image: str
    values: dict[str, Any]


def resource_scope(resource_id: str) -> tuple[str, str]:
    parts = resource_id.strip("/").split("/")
    if len(parts) < 4:
        raise ValueError(f"Cannot parse Azure resource ID {resource_id}")
    lowered = [part.lower() for part in parts]
    try:
        subscription = parts[lowered.index("subscriptions") + 1]
        resource_group = parts[lowered.index("resourcegroups") + 1]
    except (ValueError, IndexError) as error:
        raise ValueError(f"Cannot parse Azure resource ID {resource_id}") from error
    return subscription, resource_group


def managed_identity_name(resource_id: str) -> tuple[str, str]:
    _, resource_group = resource_scope(resource_id)
    parts = resource_id.strip("/").split("/")
    lowered = [part.lower() for part in parts]
    try:
        identity_index = lowered.index("userassignedidentities")
        name = parts[identity_index + 1]
    except (ValueError, IndexError) as error:
        raise ValueError(
            f"Cannot parse managed identity resource ID {resource_id}"
        ) from error
    return resource_group, name


def build_context(
    outputs: TerraformOutputs,
    *,
    location_key: str,
    image_tag: str,
    ghcr_owner: str,
    service_account: str,
) -> AzureContext:
    cluster_id = outputs.regional("aks_cluster_ids", location_key)
    subscription_id, resource_group = resource_scope(cluster_id)
    identity_id = outputs.regional(
        "foundry_workload_identity_ids",
        location_key,
    )
    identity_resource_group, identity_name = managed_identity_name(identity_id)
    registry = f"ghcr.io/{ghcr_owner}"
    values = {
        "image": {
            "registry": registry,
            "repository": "sundae-funday",
            "tag": image_tag,
        },
        "config": {
            "OPENAI_BASE_URL": outputs.regional(
                "foundry_openai_base_urls",
                location_key,
            ),
            "OPENAI_CHAT_MODEL": outputs.regional(
                "foundry_model_deployment_names",
                location_key,
            ),
            "OPENAI_AUTH_MODE": "workload_identity",
        },
        "secret": {
            "data": {
                "APPLICATIONINSIGHTS_CONNECTION_STRING": outputs.regional(
                    "application_insights_connection_strings",
                    location_key,
                ),
                "OPENAI_API_KEY": "",
            }
        },
        "workloadIdentity": {
            "enabled": True,
            "clientId": outputs.regional(
                "foundry_workload_identity_client_ids",
                location_key,
            ),
            "serviceAccount": {
                "create": True,
                "name": service_account,
            },
        },
    }
    return AzureContext(
        subscription_id=subscription_id,
        resource_group=resource_group,
        cluster_name=outputs.regional("aks_cluster_names", location_key),
        identity_resource_group=identity_resource_group,
        identity_name=identity_name,
        image=f"{registry}/sundae-funday:{image_tag}",
        values=values,
    )


def project_version() -> str:
    with (ROOT / "pyproject.toml").open("rb") as project_file:
        return str(tomllib.load(project_file)["project"]["version"])


def default_image_tag() -> str:
    commit = run(
        ["git", "rev-parse", "--short=7", "HEAD"],
        capture_output=True,
    ).stdout.strip()
    return f"{project_version()}-{commit}"


def load_outputs(
    output_path: Path | None,
    terraform_dir: Path | None,
) -> TerraformOutputs:
    if output_path is None and terraform_dir is None:
        raise ValueError("Set TF_OUTPUT_JSON to a file or TERRAFORM_DIR.")
    if output_path is not None:
        raw = output_path.read_text()
    else:
        result = run(
            ["terraform", f"-chdir={terraform_dir}", "output", "-json"],
            capture_output=True,
        )
        raw = result.stdout
    values = json.loads(raw)
    if not isinstance(values, dict):
        raise ValueError("Terraform output JSON must contain an object")
    return TerraformOutputs(values)


def secure_write(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as output_file:
        output_file.write(content)
    path.chmod(0o600)


def render_manifest(
    context: AzureContext,
    *,
    namespace: str,
    release: str,
    working_directory: Path,
) -> tuple[Path, Path]:
    overrides = working_directory / "azure-overrides.json"
    manifest = working_directory / "azure-rendered.yaml"
    secure_write(overrides, json.dumps(context.values))
    result = run(
        [
            "helm",
            "template",
            release,
            str(CHART),
            "--namespace",
            namespace,
            "--values",
            str(AZURE_VALUES),
            "--values",
            str(overrides),
        ],
        capture_output=True,
    )
    secure_write(manifest, result.stdout)
    return overrides, manifest


def validate_federation(
    context: AzureContext,
    *,
    namespace: str,
    service_account: str,
) -> None:
    issuer = run(
        [
            "az",
            "aks",
            "show",
            "--resource-group",
            context.resource_group,
            "--name",
            context.cluster_name,
            "--query",
            "oidcIssuerProfile.issuerUrl",
            "--output",
            "tsv",
        ],
        capture_output=True,
    ).stdout.strip()
    credentials = json.loads(
        run(
            [
                "az",
                "identity",
                "federated-credential",
                "list",
                "--resource-group",
                context.identity_resource_group,
                "--identity-name",
                context.identity_name,
                "--output",
                "json",
            ],
            capture_output=True,
        ).stdout
    )
    subject = f"system:serviceaccount:{namespace}:{service_account}"
    matching = any(
        credential.get("issuer") == issuer
        and credential.get("subject") == subject
        and "api://AzureADTokenExchange" in credential.get("audiences", [])
        for credential in credentials
        if isinstance(credential, dict)
    )
    if not matching:
        raise RuntimeError(
            f"Managed identity {context.identity_name} has no federated credential "
            f"for issuer {issuer}, subject {subject}, and Azure AD token exchange."
        )


def deploy(
    context: AzureContext,
    *,
    overrides: Path,
    namespace: str,
    release: str,
    service_account: str,
) -> str:
    run(["az", "account", "set", "--subscription", context.subscription_id])
    run(
        [
            "az",
            "aks",
            "get-credentials",
            "--resource-group",
            context.resource_group,
            "--name",
            context.cluster_name,
            "--overwrite-existing",
        ]
    )
    validate_federation(
        context,
        namespace=namespace,
        service_account=service_account,
    )
    run(
        [
            "helm",
            "upgrade",
            "--install",
            release,
            str(CHART),
            "--namespace",
            namespace,
            "--create-namespace",
            "--values",
            str(AZURE_VALUES),
            "--values",
            str(overrides),
            "--wait",
        ]
    )
    for deployment in ("sundae-mcp", "ops-agent", "concierge"):
        run(
            [
                "kubectl",
                "rollout",
                "status",
                f"deployment/{deployment}",
                "--namespace",
                namespace,
            ]
        )
    service = json.loads(
        run(
            [
                "kubectl",
                "get",
                "service",
                "concierge",
                "--namespace",
                namespace,
                "--output",
                "json",
            ],
            capture_output=True,
        ).stdout
    )
    ingress = service.get("status", {}).get("loadBalancer", {}).get("ingress", [])
    if not ingress:
        raise RuntimeError("Concierge LoadBalancer has no published endpoint")
    return str(ingress[0].get("ip") or ingress[0].get("hostname") or "")


def env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tf-output-json", default=os.getenv("TF_OUTPUT_JSON"))
    parser.add_argument("--terraform-dir", default=os.getenv("TERRAFORM_DIR"))
    parser.add_argument(
        "--azure-location-key",
        default=os.getenv("AZURE_LOCATION_KEY", DEFAULT_LOCATION_KEY),
    )
    parser.add_argument("--image-tag", default=os.getenv("IMAGE_TAG"))
    parser.add_argument(
        "--ghcr-owner",
        default=os.getenv("GHCR_OWNER", "pauldotyu"),
    )
    parser.add_argument(
        "--namespace",
        default=os.getenv("K8S_NAMESPACE", "demo"),
    )
    parser.add_argument(
        "--service-account",
        default=os.getenv("WORKLOAD_IDENTITY_SERVICE_ACCOUNT", "demo"),
    )
    parser.add_argument(
        "--release",
        default=os.getenv("HELM_RELEASE", "sundae-funday"),
    )
    parser.add_argument(
        "--rendered-manifest-path",
        default=os.getenv("RENDERED_MANIFEST_PATH"),
    )
    parser.add_argument(
        "--render-only",
        action="store_true",
        default=env_flag("RENDER_ONLY"),
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    outputs = load_outputs(
        Path(args.tf_output_json) if args.tf_output_json else None,
        Path(args.terraform_dir) if args.terraform_dir else None,
    )
    image_tag = args.image_tag or default_image_tag()
    context = build_context(
        outputs,
        location_key=args.azure_location_key,
        image_tag=image_tag,
        ghcr_owner=args.ghcr_owner,
        service_account=args.service_account,
    )
    with tempfile.TemporaryDirectory(prefix="sundae-funday-azure-") as temp:
        working_directory = Path(temp)
        working_directory.chmod(0o700)
        overrides, manifest = render_manifest(
            context,
            namespace=args.namespace,
            release=args.release,
            working_directory=working_directory,
        )
        if args.rendered_manifest_path:
            destination = Path(args.rendered_manifest_path)
            shutil.copyfile(manifest, destination)
            destination.chmod(0o600)
            print(f"Rendered manifest written to {destination}.")
        if args.render_only:
            print(
                "Azure manifest rendered successfully for "
                f"{context.cluster_name} with image {context.image}."
            )
            return 0
        endpoint = deploy(
            context,
            overrides=overrides,
            namespace=args.namespace,
            release=args.release,
            service_account=args.service_account,
        )
    print(f"Image: {context.image}")
    print(f"Concierge endpoint: http://{endpoint}/")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, RuntimeError) as error:
        raise SystemExit(str(error)) from error
