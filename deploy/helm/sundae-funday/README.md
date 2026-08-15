# Sundae Funday Helm chart

This chart is the canonical Kubernetes definition for the three workloads. Each
Deployment uses the same image and selects its service through `SERVICE` and
`PORT`.

Default render:

```bash
helm template sundae-funday deploy/helm/sundae-funday --namespace demo
```

Local Kind profile:

```bash
helm upgrade --install sundae-funday deploy/helm/sundae-funday \
  --namespace demo \
  --create-namespace \
  --values deploy/helm/values-local.yaml
```

Azure profile:

```bash
helm template sundae-funday deploy/helm/sundae-funday \
  --namespace demo \
  --values deploy/helm/values-azure.yaml
```

Use `secret.existingSecret` with `secret.create: false` to supply an existing
Secret. Non-secret runtime values belong under `config`; API keys and connection
strings belong under `secret.data`.

For AKS, set `workloadIdentity.enabled`, `workloadIdentity.clientId`, and the
service account values. `scripts/azure_deploy.py` derives these from Terraform
and installs the chart securely.
