#!/usr/bin/env bash
set -euo pipefail

RESOURCE_GROUP="${RESOURCE_GROUP:-rg-sundae-funday}"
LOCATION="${LOCATION:-eastus}"
ACR_NAME="${ACR_NAME:-sundaefundayacr}"
AKS_NAME="${AKS_NAME:-aks-sundae-funday}"
APP_INSIGHTS_NAME="${APP_INSIGHTS_NAME:-appi-sundae-funday}"
AMW_NAME="${AMW_NAME:-amw-sundae-funday}"
LAW_NAME="${LAW_NAME:-law-sundae-funday}"
NODE_COUNT="${NODE_COUNT:-1}"
KUBERNETES_VERSION="${KUBERNETES_VERSION:-1.31}"

az group create --name "$RESOURCE_GROUP" --location "$LOCATION"
az monitor account create \
  --name "$AMW_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION"
az monitor log-analytics workspace create \
  --resource-group "$RESOURCE_GROUP" \
  --workspace-name "$LAW_NAME" \
  --location "$LOCATION"
az acr create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$ACR_NAME" \
  --sku Basic \
  --admin-enabled false
az monitor app-insights component create \
  --app "$APP_INSIGHTS_NAME" \
  --resource-group "$RESOURCE_GROUP" \
  --location "$LOCATION" \
  --application-type web

AMW_ID="$(az monitor account show --name "$AMW_NAME" --resource-group "$RESOURCE_GROUP" --query id -o tsv)"
LAW_ID="$(az monitor log-analytics workspace show --resource-group "$RESOURCE_GROUP" --workspace-name "$LAW_NAME" --query id -o tsv)"

az aks create \
  --resource-group "$RESOURCE_GROUP" \
  --name "$AKS_NAME" \
  --location "$LOCATION" \
  --node-count "$NODE_COUNT" \
  --enable-managed-identity \
  --generate-ssh-keys \
  --kubernetes-version "$KUBERNETES_VERSION"
az aks update \
  --resource-group "$RESOURCE_GROUP" \
  --name "$AKS_NAME" \
  --attach-acr "$ACR_NAME"
az aks update \
  --resource-group "$RESOURCE_GROUP" \
  --name "$AKS_NAME" \
  --enable-azure-monitor-metrics \
  --azure-monitor-workspace-resource-id "$AMW_ID"
az aks enable-addons \
  --resource-group "$RESOURCE_GROUP" \
  --name "$AKS_NAME" \
  --addons monitoring \
  --workspace-resource-id "$LAW_ID"

echo "Setup complete."
echo "Resource group: $RESOURCE_GROUP"
echo "ACR: $ACR_NAME"
echo "AKS: $AKS_NAME"
echo "Application Insights: $APP_INSIGHTS_NAME"
