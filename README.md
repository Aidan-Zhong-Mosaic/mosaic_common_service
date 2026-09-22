# Insurance Data Gateway

A Python/FastAPI backend exposing one IAM-authenticated endpoint for running SQL
against our Redshift insurance data warehouse, so any microservice (regardless of
language) can call it instead of connecting to Redshift directly.

Read **[ARCHITECTURE.md](./ARCHITECTURE.md)** first — it covers why this is a
gateway service rather than every microservice connecting directly, the current
network-path-to-Redshift situation (a personal Client VPN certificate is being used
as a stopgap), and what's *not* enforced given the single raw-SQL endpoint.

## What it exposes

- `POST /query` — takes `{"sql": "<string>"}`, runs it against the pooled Redshift
  connection, returns `{"rows": [...], "row_count": N}`.
- `GET /health` — health check.

**The connection never sets a schema or `search_path`.** Every SQL string sent to
`/query` must fully qualify tables as `schema.table` — there is no "current" schema
to fall back on.

## Calling it

No client library needed — it's a plain REST API behind API Gateway's `AWS_IAM`
authorizer. Any AWS SDK can sign a request with SigV4 using the calling service's
own IAM role/credentials (`service = execute-api`) and call it directly:

```
POST https://<api-id>.execute-api.eu-west-1.amazonaws.com/query
{"sql": "SELECT * FROM insurance.policies WHERE policy_id = '123'"}
```

## Local dev

```
pip install -r requirements.txt
uvicorn app.main:app --reload
```

Locally there is no API Gateway in front of the service, so `auth.py` requires the
`x-verified-caller-arn` header directly — set it to one of the ARNs registered in
`app/governance/access_control.py` to exercise access control locally, e.g.:

```
curl -X POST localhost:8000/query \
  -H 'x-verified-caller-arn: arn:aws:iam::111111111111:role/mosaic-ai-chat' \
  -H 'content-type: application/json' \
  -d '{"sql": "SELECT * FROM insurance.policies LIMIT 1"}'
```

Also requires network access to Redshift (VPN/peering/PrivateLink) and a local DB
credentials file.

## DB credentials

Redshift here uses plain username/password auth (not IAM database auth), so the
gateway needs a local JSON file — copy `redshift-credentials.example.json` to the
path pointed at by `GATEWAY_REDSHIFT_CREDENTIALS_FILE` (default
`/etc/insurance-data-gateway/redshift-credentials.json`), fill in the real
password, and `chmod 600` it:

```json
{"username": "svc_insurance_data_gateway", "password": "..."}
```

The gateway refuses to start if that file is missing or is readable by group/other
(`app/redshift_client.py`). **Never commit the real file** — it's already covered
by `.gitignore`, but double check before pushing.

## Deploying

Holds a real connection pool, so it runs as a long-lived process, not Lambda — see
`deploy/README.md` for a systemd (EC2) example and the recommended ECS/Fargate path.

## Requesting access for a new service

1. Give the platform team the service's IAM role ARN.
2. Platform team adds a grant for that role in `app/governance/access_control.py`
   (this only gates whether the caller can hit `/query` at all - see
   ARCHITECTURE.md for why there's no per-table/per-query control right now).
3. Attach `policies/example-consumer-iam-policy.json` (with the real API ID filled
   in) to that role — invoke-only, this only lets them call the gateway's HTTP API,
   not touch Redshift or the credentials file directly.
