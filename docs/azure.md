# Azure AKS deployment path

This repo keeps the app images and manifests the same between local Docker Compose and AKS. The switch for observability is environment driven:

- If `APPLICATIONINSIGHTS_CONNECTION_STRING` is empty, the apps use standard OTLP exporters.
- If `APPLICATIONINSIGHTS_CONNECTION_STRING` is set, the apps send traces, metrics, and logs to Azure Monitor exporters.

## Prerequisites

- Azure resources provisioned with the required Terraform outputs below
- Azure CLI authenticated to the target tenant
- `kubectl`, `kustomize`, Terraform, Python 3, and Git
- Three public GHCR images published for the selected commit tag, or equivalent
  Kubernetes image pull credentials for private packages

## What the scripts do

The Azure overlay deploys commit-tagged images from GitHub Container Registry.
The CI workflow publishes all three images after a push using the seven-character
Git commit as the tag.

`scripts/azure-deploy.sh` reads Terraform outputs, renders the sensitive
Application Insights connection string into a Kubernetes Secret, configures
Microsoft Foundry workload identity, applies the manifests, waits for rollouts,
and prints the concierge endpoint.

The Azure overlay reads:

- `deploy/k8s/overlays/azure/azure.config.env`
- `deploy/k8s/overlays/azure/azure.secret.env`

Copy the tracked example files when configuring the overlay manually:

```bash
cp deploy/k8s/overlays/azure/azure.config.env.example \
  deploy/k8s/overlays/azure/azure.config.env
cp deploy/k8s/overlays/azure/azure.secret.env.example \
  deploy/k8s/overlays/azure/azure.secret.env
```

The examples use recognizable dummy GUIDs and `*-example` resource names while
preserving the expected Azure value formats. Replace every dummy value before
rendering. The runtime files match the root `azure.*.env` ignore rule, while the
`.example` files are committed. You can then run:

```bash
kustomize build deploy/k8s/overlays/azure
```

The deployment script regenerates both files from Terraform outputs before
building the overlay.

## Required Terraform outputs

The renderer consumes these outputs:

- `aks_cluster_ids`
- `aks_cluster_names`
- `application_insights_connection_strings`
- `application_insights_resource_ids`
- `azure_monitor_workspace_ids`
- `foundry_account_ids`
- `foundry_model_deployment_names`
- `foundry_openai_base_urls`
- `foundry_workload_identity_client_ids`
- `foundry_workload_identity_ids`
- `foundry_workload_identity_principal_ids`
- `log_analytics_workspace_ids`
- `otel_logs_endpoints`
- `otel_metrics_endpoints`

The Azure ConfigMap also records the non-secret resource IDs supplied by the
Terraform deployment for diagnostics.

The federated identity must trust this service account subject:

```bash
system:serviceaccount:demo:demo
```

It must use the AKS OIDC issuer and the
`api://AzureADTokenExchange` audience. The deployment script verifies this
federation before applying the manifests.

## Render and deploy

```bash
terraform -chdir=/path/to/terraform output -json > /tmp/sundae-outputs.json
TF_OUTPUT_JSON=/tmp/sundae-outputs.json bash scripts/azure-deploy.sh
```

You can also let the script invoke Terraform:

```bash
TERRAFORM_DIR=/path/to/terraform bash scripts/azure-deploy.sh
```

`IMAGE_TAG` defaults to the current seven-character Git commit. Override it only
when deploying a different published GHCR tag. Set `AZURE_LOCATION_KEY` when the
Terraform outputs contain more than one region. Forks should set `GHCR_OWNER` to
the GitHub account that owns their published packages.

Push the commit and wait for the `container-build` CI jobs to publish all three
images before deploying:

```bash
git push
TF_OUTPUT_JSON=/tmp/sundae-outputs.json \
GHCR_OWNER=your-github-user \
bash scripts/azure-deploy.sh
```

To render without connecting to AKS:

```bash
TF_OUTPUT_JSON=/tmp/sundae-outputs.json \
RENDER_ONLY=true \
RENDERED_MANIFEST_PATH=/tmp/sundae-azure.yaml \
bash scripts/azure-deploy.sh
```

The rendered file contains the sensitive Application Insights connection string.
Keep it outside the repository and delete it after use.

## Useful checks

```bash
kubectl get pods -n demo
kubectl get svc -n demo
kubectl logs deployment/concierge -n demo
kubectl logs deployment/ops-agent -n demo
kubectl logs deployment/sundae-mcp -n demo
```

## Notes

- The Application Insights connection string and optional model API key are
  stored in `azure-secrets`, not a ConfigMap.
- Concierge and Ops Scoop use the `demo` service account in the `demo` namespace.
- Pod annotations expose `/metrics` for managed Prometheus scraping.
- The apps do not need a database or message broker, so AKS setup stays small.
