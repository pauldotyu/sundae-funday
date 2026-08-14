# Azure AKS deployment path

This repo keeps the app images and manifests the same between local Docker Compose and AKS. The switch for observability is environment driven:

- If `APPLICATIONINSIGHTS_CONNECTION_STRING` is empty, the apps use standard OTLP exporters.
- If `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, the apps send traces, metrics, and logs to Azure Monitor exporters.

## What the scripts do

`scripts/azure-setup.sh` creates a resource group, Azure Container Registry, Azure Monitor workspace, Log Analytics workspace, Application Insights component, and AKS cluster. It also attaches ACR and enables managed Prometheus plus Container Insights.

`scripts/azure-deploy.sh` builds the three images with ACR Tasks, renders the Azure overlay with your registry and model settings, applies the manifests, waits for rollouts, and prints the concierge endpoint.

## Required environment variables

```bash
export RESOURCE_GROUP=rg-sundae-funday
export LOCATION=eastus
export ACR_NAME=sundaefundayacr
export AKS_NAME=aks-sundae-funday
export APP_INSIGHTS_NAME=appi-sundae-funday
export OPENAI_BASE_URL="https://<your-endpoint>/openai/v1/"
export OPENAI_CHAT_MODEL="gpt-4.1-mini"
export OPENAI_API_KEY="<your-api-key>"
```

Use Azure OpenAI, Azure AI Foundry, or another OpenAI compatible endpoint. This demo keeps the Azure overlay on API key auth to stay compact. If you want workload identity or Entra auth for model access, wire a service account and federation in your own overlay.

## Setup

```bash
bash scripts/azure-setup.sh
```

## Build and deploy

```bash
bash scripts/azure-deploy.sh
```

## Useful checks

```bash
kubectl get pods -n sundae-funday
kubectl get svc -n sundae-funday
kubectl logs deployment/concierge -n sundae-funday
kubectl logs deployment/ops-agent -n sundae-funday
kubectl logs deployment/sundae-mcp -n sundae-funday
```

## Notes

- `deploy/k8s/overlays/azure` keeps image and config placeholders so the same base stays reusable.
- Pod annotations expose `/metrics` for managed Prometheus scraping.
- The apps do not need a database or message broker, so AKS setup stays small.
