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
docker compose up -d otel-collector loki tempo prometheus grafana
helm upgrade --install sundae-funday deploy/helm/sundae-funday \
  --namespace demo \
  --create-namespace \
  --values deploy/helm/values-local.yaml
```

The local values use `172.17.0.1` for host Ollama and OTLP. Override
`config.OPENAI_BASE_URL` and `config.OTEL_EXPORTER_OTLP_ENDPOINT` when Docker
uses a different host address.

### Local Kind networking

`values-local.yaml` enables `hostNetwork` for all three pods. They share the
Kind node network namespace, use distinct ports, and call each other through
`127.0.0.1`. `ClusterFirstWithHostNet` preserves Kubernetes DNS resolution.

The Kind node runs inside Docker, so `172.17.0.1` is its default bridge route
to Ollama and the Compose OTLP collector on the host. This address varies by
Docker setup and can be overridden through Helm values or the root Makefile.

The concierge uses NodePort `30001`; `deploy/kind-config.yaml` maps that node
port to host port `8301`. This host-network topology keeps the local demo small.
The default and Azure values retain normal pod networking.

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
