# Sundae Funday Helm chart

The chart deploys the same three application workloads and services as the Kustomize manifests. The image tag defaults to `Chart.appVersion`, so the published chart automatically uses images built from the same commit.

```bash
helm install sundae-funday deploy/helm/sundae-funday \
  --namespace demo \
  --create-namespace
```

For Azure, copy `deploy/helm/azure.example.values.yaml` to the ignored `deploy/helm/azure.values.yaml`, populate its values, and install with:

```bash
helm upgrade --install sundae-funday deploy/helm/sundae-funday \
  --namespace demo \
  --create-namespace \
--values deploy/helm/azure.values.yaml
```

For AKS workload identity, set:

```yaml
workloadIdentity:
  enabled: true
  clientId: 11111111-1111-1111-1111-111111111111
```

Set non-secret application settings under `config`. Set secret values under `secret.data`, or configure `secret.existingSecret` and `secret.create: false`.

Published charts are available at:

```text
oci://ghcr.io/pauldotyu/charts/sundae-funday
```

Terraform can install a published version with:

```hcl
resource "helm_release" "sundae_funday" {
  name             = "sundae-funday"
  namespace        = "demo"
  create_namespace = true
  repository       = "oci://ghcr.io/pauldotyu/charts"
  chart            = "sundae-funday"
  version          = "0.0.0-abcdef0"

  set = [
    {
      name  = "workloadIdentity.enabled"
      value = "true"
    },
    {
      name  = "workloadIdentity.clientId"
      value = azurerm_user_assigned_identity.foundry.client_id
    }
  ]
}
```
