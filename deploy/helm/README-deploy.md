# Deploying mosaic-common-service to Kubernetes

The shared backend other services call into for cross-cutting capabilities.
Governed Redshift access is what it does today - the steps below are
specific to that capability, and are expected to grow a section per
capability (MCP tool calls, LLM/SLM routing, shared algorithms) as those
get built, rather than becoming their own services.

## One-time setup

1. Build and push the image to your registry (ECR shown here):
   ```bash
   aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 123456789012.dkr.ecr.us-east-1.amazonaws.com
   docker build -t 123456789012.dkr.ecr.us-east-1.amazonaws.com/mosaic-common-service:0.3.0 .
   docker push 123456789012.dkr.ecr.us-east-1.amazonaws.com/mosaic-common-service:0.3.0
   ```

2. Create the Redshift credentials Secret out-of-band (never through Helm values) - needed for today's Redshift capability specifically:
   ```bash
   kubectl create namespace mosaic
   kubectl -n mosaic create secret generic mosaic-common-service-redshift-credentials \
     --from-literal=credentials.json='{"username":"svc_insurance_data_gateway","password":"REPLACE_ME"}'
   ```
   Longer term, swap this for the External Secrets Operator syncing from AWS
   Secrets Manager, so the password lives only in Secrets Manager and rotates
   without a kubectl command.

3. Copy `values-prod.yaml.example` to `values-prod.yaml` (gitignored) and fill in
   the image repo/tag, `config.redshiftHost`, and the secret name from step 2.

## Install / upgrade

```bash
helm upgrade --install mosaic-common-service ./mosaic-common-service \
  -n mosaic --create-namespace \
  -f mosaic-common-service/values-prod.yaml
```

`helm upgrade --install` is idempotent - same command for the first install and
every later change.

## Verify

```bash
kubectl -n mosaic rollout status deployment/mosaic-common-service
kubectl -n mosaic port-forward svc/mosaic-common-service 8339:8339
curl localhost:8339/health
```

## Rollback

```bash
helm -n mosaic history mosaic-common-service
helm -n mosaic rollback mosaic-common-service <revision>
```
