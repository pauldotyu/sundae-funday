#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-sundae-funday}"
ACR_NAME="${ACR_NAME:-sundaefundayacr}"
AKS_NAME="${AKS_NAME:-aks-sundae-funday}"
APP_INSIGHTS_NAME="${APP_INSIGHTS_NAME:-appi-sundae-funday}"
OPENAI_BASE_URL="${OPENAI_BASE_URL:?Set OPENAI_BASE_URL to your Azure OpenAI or Foundry endpoint}"
OPENAI_CHAT_MODEL="${OPENAI_CHAT_MODEL:?Set OPENAI_CHAT_MODEL}"
OPENAI_API_KEY="${OPENAI_API_KEY:?Set OPENAI_API_KEY}"
IMAGE_TAG="${IMAGE_TAG:-0.1.0}"

ACR_LOGIN_SERVER="$(az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" --query loginServer -o tsv)"
APP_INSIGHTS_CONNECTION_STRING="$(az monitor app-insights component show --app "$APP_INSIGHTS_NAME" --resource-group "$RESOURCE_GROUP" --query connectionString -o tsv)"
export ACR_LOGIN_SERVER
export APP_INSIGHTS_CONNECTION_STRING

for service in sundae-mcp ops-agent concierge; do
  az acr build \
    --registry "$ACR_NAME" \
    --image "$service:$IMAGE_TAG" \
    --build-arg "SERVICE=$service" \
    .
done

az aks get-credentials --resource-group "$RESOURCE_GROUP" --name "$AKS_NAME" --overwrite-existing
mkdir -p deploy/k8s/rendered
kubectl kustomize deploy/k8s/overlays/azure | python3 - <<'PY' > deploy/k8s/rendered/azure.yaml
import os
import sys

manifest = sys.stdin.read()
replacements = {
    "REPLACE_ME_ACR": os.environ["ACR_LOGIN_SERVER"],
    "REPLACE_ME_APPINSIGHTS_CONNECTION_STRING": os.environ["APP_INSIGHTS_CONNECTION_STRING"],
    "REPLACE_ME_OPENAI_BASE_URL": os.environ["OPENAI_BASE_URL"],
    "REPLACE_ME_OPENAI_CHAT_MODEL": os.environ["OPENAI_CHAT_MODEL"],
    "REPLACE_ME_OPENAI_API_KEY": os.environ["OPENAI_API_KEY"],
}
for needle, value in replacements.items():
    manifest = manifest.replace(needle, value)
print(manifest)
PY
kubectl apply -f deploy/k8s/rendered/azure.yaml
kubectl rollout status deployment/sundae-mcp -n sundae-funday
kubectl rollout status deployment/ops-agent -n sundae-funday
kubectl rollout status deployment/concierge -n sundae-funday

HOST="$(kubectl get service concierge -n sundae-funday -o jsonpath='{.status.loadBalancer.ingress[0].ip}')"
if [ -z "$HOST" ]; then
  HOST="$(kubectl get service concierge -n sundae-funday -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')"
fi

echo "Concierge endpoint: http://$HOST/"
