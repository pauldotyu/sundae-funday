# Azure AKS deployment

Azure resources are provisioned separately with Terraform. This repository
renders and installs the application Helm chart.

## Prerequisites

- Azure CLI authenticated to the target tenant
- Terraform outputs from the infrastructure deployment
- `helm`, `kubectl`, Python 3.12, uv, and Git
- The selected shared image published to GHCR

CI publishes:

```text
ghcr.io/<owner>/sundae-funday:<project-version>-<short-commit>
```

The former service-specific image names are aliases for the same image digest.

## Required Terraform outputs

The deployer reads the Terraform JSON once and consumes:

- `aks_cluster_ids`
- `aks_cluster_names`
- `application_insights_connection_strings`
- `foundry_model_deployment_names`
- `foundry_openai_base_urls`
- `foundry_workload_identity_client_ids`
- `foundry_workload_identity_ids`

Outputs may be direct values or maps keyed by Azure location. Set
`AZURE_LOCATION_KEY` when a map contains multiple regions.

Unused Azure resource IDs and direct OTLP endpoint values are no longer copied
into the application ConfigMap.

## Workload identity

The managed identity must have a federated credential for:

```text
issuer:   the AKS OIDC issuer
subject:  system:serviceaccount:<namespace>:<service-account>
audience: api://AzureADTokenExchange
```

Defaults are namespace `demo` and service account `demo`. The deployer validates
the federation before installation.

## Render only

```bash
TF_OUTPUT_JSON=/tmp/sundae-outputs.json \
RENDER_ONLY=true \
RENDERED_MANIFEST_PATH=/tmp/sundae-azure.yaml \
uv run python scripts/azure_deploy.py
```

The generated values and intermediate manifest live in a mode `0700` temporary
directory. A requested output manifest is mode `0600` because it contains the
Application Insights connection string.

The image tag defaults to `<project-version>-<short-commit>`, derived from
`pyproject.toml` and Git. Override `IMAGE_TAG` only when deploying another
published tag. Forks should set `GHCR_OWNER`.

## Deploy

From an output file:

```bash
TF_OUTPUT_JSON=/tmp/sundae-outputs.json \
GHCR_OWNER=your-github-user \
make azure-deploy
```

Or let the deployer invoke Terraform:

```bash
TERRAFORM_DIR=/path/to/terraform make azure-deploy
```

The deployer:

1. renders `deploy/helm/values-azure.yaml` with secure generated overrides;
2. validates AKS workload identity federation;
3. runs `helm upgrade --install --wait`;
4. waits for all three Deployments;
5. prints the shared image and concierge LoadBalancer endpoint.

Environment overrides:

| Variable | Default |
| --- | --- |
| `AZURE_LOCATION_KEY` | `West US 3` |
| `GHCR_OWNER` | `pauldotyu` |
| `K8S_NAMESPACE` | `demo` |
| `WORKLOAD_IDENTITY_SERVICE_ACCOUNT` | `demo` |
| `HELM_RELEASE` | `sundae-funday` |

## Checks

```bash
helm lint deploy/helm/sundae-funday \
  --values deploy/helm/values-azure.yaml
kubectl get pods,services -n demo
kubectl logs deployment/concierge -n demo
kubectl logs deployment/ops-agent -n demo
kubectl logs deployment/sundae-mcp -n demo
```
