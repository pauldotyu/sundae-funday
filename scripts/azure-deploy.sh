#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TF_OUTPUT_JSON="${TF_OUTPUT_JSON:-}"
TERRAFORM_DIR="${TERRAFORM_DIR:-}"
AZURE_LOCATION_KEY="${AZURE_LOCATION_KEY:-West US 3}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short=7 HEAD)}"
GHCR_OWNER="${GHCR_OWNER:-pauldotyu}"
K8S_NAMESPACE="${K8S_NAMESPACE:-demo}"
WORKLOAD_IDENTITY_SERVICE_ACCOUNT="${WORKLOAD_IDENTITY_SERVICE_ACCOUNT:-demo}"
RENDER_ONLY="${RENDER_ONLY:-false}"
RENDERED_MANIFEST_PATH="${RENDERED_MANIFEST_PATH:-}"

if [[ -z "$TF_OUTPUT_JSON" && -z "$TERRAFORM_DIR" ]]; then
  echo "Set TF_OUTPUT_JSON to a terraform output -json file or TERRAFORM_DIR." >&2
  exit 1
fi

mkdir -p deploy/k8s/rendered
TF_OUTPUT_TEMP=""
if [[ -z "$TF_OUTPUT_JSON" ]]; then
  TF_OUTPUT_TEMP="$(mktemp deploy/k8s/rendered/terraform-output.XXXXXX.json)"
  terraform -chdir="$TERRAFORM_DIR" output -json > "$TF_OUTPUT_TEMP"
  TF_OUTPUT_JSON="$TF_OUTPUT_TEMP"
fi

export AZURE_LOCATION_KEY GHCR_OWNER IMAGE_TAG
python3 - "$TF_OUTPUT_JSON" <<'PY'
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

outputs = json.loads(Path(sys.argv[1]).read_text())
location_key = os.environ["AZURE_LOCATION_KEY"]


def output(name: str) -> Any:
    entry = outputs.get(name)
    if entry is None:
        raise SystemExit(f"Terraform output is missing {name!r}")
    if isinstance(entry, dict) and "value" in entry:
        return entry["value"]
    return entry


def regional(name: str) -> str:
    value = output(name)
    if not isinstance(value, dict):
        return str(value)
    if location_key in value:
        return str(value[location_key])
    if len(value) == 1:
        return str(next(iter(value.values())))
    available = ", ".join(sorted(value))
    raise SystemExit(
        f"Terraform output {name!r} has multiple regions. "
        f"Set AZURE_LOCATION_KEY to one of: {available}"
    )


cluster_id = regional("aks_cluster_ids")
match = re.match(
    r"^/subscriptions/([^/]+)/resourceGroups/([^/]+)/",
    cluster_id,
    flags=re.IGNORECASE,
)
if match is None:
    raise SystemExit(f"Cannot parse subscription and resource group from {cluster_id}")
subscription_id, resource_group = match.groups()

config = {
    "APP_VERSION": os.environ["IMAGE_TAG"],
    "OTEL_EXPORTER_OTLP_ENDPOINT": "",
    "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT": regional("otel_logs_endpoints"),
    "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": regional("otel_metrics_endpoints"),
    "OPENAI_BASE_URL": regional("foundry_openai_base_urls"),
    "OPENAI_CHAT_MODEL": regional("foundry_model_deployment_names"),
    "OPENAI_AUTH_MODE": "workload_identity",
    "AZURE_SUBSCRIPTION_ID": subscription_id,
    "AZURE_RESOURCE_GROUP": resource_group,
    "AZURE_LOCATION": re.sub(r"[^a-z0-9]", "", location_key.lower()),
    "AKS_CLUSTER_ID": cluster_id,
    "AKS_CLUSTER_NAME": regional("aks_cluster_names"),
    "APPLICATIONINSIGHTS_RESOURCE_ID": regional(
        "application_insights_resource_ids"
    ),
    "AZURE_MONITOR_WORKSPACE_ID": regional("azure_monitor_workspace_ids"),
    "FOUNDRY_ACCOUNT_ID": regional("foundry_account_ids"),
    "FOUNDRY_WORKLOAD_IDENTITY_ID": regional(
        "foundry_workload_identity_ids"
    ),
    "FOUNDRY_WORKLOAD_IDENTITY_CLIENT_ID": regional(
        "foundry_workload_identity_client_ids"
    ),
    "FOUNDRY_WORKLOAD_IDENTITY_PRINCIPAL_ID": regional(
        "foundry_workload_identity_principal_ids"
    ),
    "LOG_ANALYTICS_WORKSPACE_ID": regional("log_analytics_workspace_ids"),
}
secrets = {
    "APPLICATIONINSIGHTS_CONNECTION_STRING": regional(
        "application_insights_connection_strings"
    ),
    "OPENAI_API_KEY": "",
}

overlay = Path("deploy/k8s/overlays/azure")
for path, values in (
    (overlay / "azure.config.env", config),
    (overlay / "azure.secret.env", secrets),
):
    path.write_text("".join(f"{key}={value}\n" for key, value in values.items()))
    path.chmod(0o600)
PY

SOURCE_MANIFEST="$(mktemp deploy/k8s/rendered/azure-source.XXXXXX.yaml)"
RENDERED_MANIFEST="$(mktemp deploy/k8s/rendered/azure-rendered.XXXXXX.yaml)"
chmod 600 "$SOURCE_MANIFEST" "$RENDERED_MANIFEST"
trap 'rm -f "$SOURCE_MANIFEST" "$RENDERED_MANIFEST" "$TF_OUTPUT_TEMP"' EXIT

kustomize build deploy/k8s/overlays/azure > "$SOURCE_MANIFEST"

DEPLOYMENT_METADATA="$(
  python3 - "$TF_OUTPUT_JSON" "$SOURCE_MANIFEST" "$RENDERED_MANIFEST" <<'PY'
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

outputs = json.loads(Path(sys.argv[1]).read_text())
manifest = Path(sys.argv[2]).read_text()
location_key = os.environ["AZURE_LOCATION_KEY"]


def output(name: str) -> Any:
    entry = outputs.get(name)
    if entry is None:
        raise SystemExit(f"Terraform output is missing {name!r}")
    if isinstance(entry, dict) and "value" in entry:
        return entry["value"]
    return entry


def regional(name: str) -> str:
    value = output(name)
    if not isinstance(value, dict):
        return str(value)
    if location_key in value:
        return str(value[location_key])
    if len(value) == 1:
        return str(next(iter(value.values())))
    available = ", ".join(sorted(value))
    raise SystemExit(
        f"Terraform output {name!r} has multiple regions. "
        f"Set AZURE_LOCATION_KEY to one of: {available}"
    )


cluster_id = regional("aks_cluster_ids")
cluster_name = regional("aks_cluster_names")
identity_id = regional("foundry_workload_identity_ids")
match = re.match(
    r"^/subscriptions/([^/]+)/resourceGroups/([^/]+)/",
    cluster_id,
    flags=re.IGNORECASE,
)
if match is None:
    raise SystemExit(f"Cannot parse subscription and resource group from {cluster_id}")
subscription_id, resource_group = match.groups()
identity_match = re.match(
    r"^/subscriptions/[^/]+/resourceGroups/([^/]+)/providers/"
    r"Microsoft\.ManagedIdentity/userAssignedIdentities/([^/]+)$",
    identity_id,
    flags=re.IGNORECASE,
)
if identity_match is None:
    raise SystemExit(f"Cannot parse managed identity resource ID {identity_id}")
identity_resource_group, identity_name = identity_match.groups()

manifest = re.sub(
    r"ghcr\.io/[^/]+/(sundae-funday-(?:sundae-mcp|ops-agent|concierge))"
    r":[^\s]+",
    rf"ghcr.io/{os.environ['GHCR_OWNER']}/\1:{os.environ['IMAGE_TAG']}",
    manifest,
)

Path(sys.argv[3]).write_text(manifest)
print(
    json.dumps(
        {
            "subscription_id": subscription_id,
            "resource_group": resource_group,
            "cluster_name": cluster_name,
            "identity_resource_group": identity_resource_group,
            "identity_name": identity_name,
            "image_tag": os.environ["IMAGE_TAG"],
        }
    )
)
PY
)"

if [[ -n "$RENDERED_MANIFEST_PATH" ]]; then
  install -m 600 "$RENDERED_MANIFEST" "$RENDERED_MANIFEST_PATH"
  echo "Rendered manifest written to $RENDERED_MANIFEST_PATH."
fi

readarray -t DEPLOYMENT_CONTEXT < <(
  python3 -c '
import json
import sys

metadata = json.loads(sys.argv[1])
print(metadata["subscription_id"])
print(metadata["resource_group"])
print(metadata["cluster_name"])
print(metadata["identity_resource_group"])
print(metadata["identity_name"])
' "$DEPLOYMENT_METADATA"
)
SUBSCRIPTION_ID="${DEPLOYMENT_CONTEXT[0]}"
RESOURCE_GROUP="${DEPLOYMENT_CONTEXT[1]}"
AKS_NAME="${DEPLOYMENT_CONTEXT[2]}"
IDENTITY_RESOURCE_GROUP="${DEPLOYMENT_CONTEXT[3]}"
IDENTITY_NAME="${DEPLOYMENT_CONTEXT[4]}"

if [[ "$RENDER_ONLY" == "true" ]]; then
  echo "Azure manifest rendered successfully for $AKS_NAME with image tag $IMAGE_TAG."
  exit 0
fi

az account set --subscription "$SUBSCRIPTION_ID"
az aks get-credentials \
  --resource-group "$RESOURCE_GROUP" \
  --name "$AKS_NAME" \
  --overwrite-existing

OIDC_ISSUER="$(
  az aks show \
    --resource-group "$RESOURCE_GROUP" \
    --name "$AKS_NAME" \
    --query oidcIssuerProfile.issuerUrl \
    --output tsv
)"
EXPECTED_SUBJECT="system:serviceaccount:${K8S_NAMESPACE}:${WORKLOAD_IDENTITY_SERVICE_ACCOUNT}"
MATCHING_FEDERATED_CREDENTIAL="$(
  az identity federated-credential list \
    --resource-group "$IDENTITY_RESOURCE_GROUP" \
    --identity-name "$IDENTITY_NAME" \
    --query "[?issuer=='${OIDC_ISSUER}' && subject=='${EXPECTED_SUBJECT}' && contains(audiences, 'api://AzureADTokenExchange')].name | [0]" \
    --output tsv
)"
if [[ -z "$MATCHING_FEDERATED_CREDENTIAL" ]]; then
  echo "Managed identity $IDENTITY_NAME has no federated credential for:" >&2
  echo "  issuer: $OIDC_ISSUER" >&2
  echo "  subject: $EXPECTED_SUBJECT" >&2
  echo "  audience: api://AzureADTokenExchange" >&2
  exit 1
fi

kubectl apply -f "$RENDERED_MANIFEST"
kubectl rollout status deployment/sundae-mcp -n "$K8S_NAMESPACE"
kubectl rollout status deployment/ops-agent -n "$K8S_NAMESPACE"
kubectl rollout status deployment/concierge -n "$K8S_NAMESPACE"

HOST="$(
  kubectl get service concierge \
    -n "$K8S_NAMESPACE" \
    -o jsonpath='{.status.loadBalancer.ingress[0].ip}'
)"
if [[ -z "$HOST" ]]; then
  HOST="$(
    kubectl get service concierge \
      -n "$K8S_NAMESPACE" \
      -o jsonpath='{.status.loadBalancer.ingress[0].hostname}'
  )"
fi

echo "Images: ghcr.io/$GHCR_OWNER/sundae-funday-<service>:$IMAGE_TAG"
echo "Concierge endpoint: http://$HOST/"
