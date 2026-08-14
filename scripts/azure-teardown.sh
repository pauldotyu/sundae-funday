#!/usr/bin/env bash
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-sundae-funday}"
ACR_NAME="${ACR_NAME:-sundaefundayacr}"
AKS_NAME="${AKS_NAME:-aks-sundae-funday}"
APP_INSIGHTS_NAME="${APP_INSIGHTS_NAME:-appi-sundae-funday}"
AMW_NAME="${AMW_NAME:-amw-sundae-funday}"
LAW_NAME="${LAW_NAME:-law-sundae-funday}"

echo "Tearing down resources in $RESOURCE_GROUP..."

# Delete AKS first (depends on ACR)
if az aks show --name "$AKS_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  echo "> Deleting AKS cluster..."
  az aks delete --name "$AKS_NAME" --resource-group "$RESOURCE_GROUP" --yes --no-wait || true
fi

# Clean ACR (requires deleting images first)
if az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  echo "> Clearing ACR..."
  IMAGES="$(az acr repository list --name "$ACR_NAME" --query '[].name' -o tsv 2>/dev/null || true)"
  for repo in $IMAGES; do
    TAGS="$(az acr repository show-tags --name "$ACR_NAME" --repository "$repo" --query '[0]' -o tsv 2>/dev/null || true)"
    if [ -n "$TAGS" ]; then
      az acr repository delete --name "$ACR_NAME" --repository "${repo}:${TAGS}" --yes 2>/dev/null || true
    fi
  done
fi

# Delete monitoring resources (depend on each other)
if az monitor account show --name "$AMW_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  echo "> Deleting Monitor Workspace..."
  az monitor account delete --name "$AMW_NAME" --resource-group "$RESOURCE_GROUP" --yes || true
fi

if az monitor log-analytics workspace show --resource-group "$RESOURCE_GROUP" --workspace-name "$LAW_NAME" &>/dev/null; then
  echo "> Deleting Log Analytics Workspace..."
  az monitor log-analytics workspace delete --resource-group "$RESOURCE_GROUP" --workspace-name "$LAW_NAME" --yes || true
fi

# Delete App Insights (depends on Monitor Workspace)
if az monitor app-insights component show --app "$APP_INSIGHTS_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  echo "> Deleting Application Insights..."
  az monitor app-insights component delete --app "$APP_INSIGHTS_NAME" --resource-group "$RESOURCE_GROUP" --yes || true
fi

# Clean up ACR again in case cleanup raced
if az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  echo "> Final ACR cleanup..."
  IMAGES="$(az acr repository list --name "$ACR_NAME" --query '[].name' -o tsv 2>/dev/null || true)"
  for repo in $IMAGES; do
    TAGS="$(az acr repository show-tags --name "$ACR_NAME" --repository "$repo" --query '[0]' -o tsv 2>/dev/null || true)"
    if [ -n "$TAGS" ]; then
      az acr repository delete --name "$ACR_NAME" --repository "${repo}:${TAGS}" --yes 2>/dev/null || true
    fi
  done
fi

# Delete resource group last
echo "> Deleting resource group $RESOURCE_GROUP..."
az group delete --name "$RESOURCE_GROUP" --yes --no-wait || true

echo "Teardown initiated. Azure resources are being deleted asynchronously."
