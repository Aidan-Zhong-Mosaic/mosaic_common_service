# Insurance Data Gateway

A Python/FastAPI backend exposing one endpoint for running SQL against our
Redshift insurance data warehouse, so any microservice (regardless of language)
can call it instead of connecting to Redshift directly.

Read **[ARCHITECTURE.md](./ARCHITECTURE.md)** first — it covers why this is a
gateway service rather than every microservice connecting directly, the current
network-path-to-Redshift situation (a personal Client VPN certificate is being used
as a stopgap), and what's *not* enforced given the single raw-SQL endpoint and no
application-level auth.

## What it exposes

- `POST /query` — takes `{"sql": "<string>"}`, runs it against the pooled Redshift
  connection, returns `{"rows": [...], "row_count": N}`.
- `GET /health` — health check.

**The connection never sets a schema or `search_path`.** Every SQL string sent to
`/query` must fully qualify tables as `schema.table` — there is no "current" schema
to fall back on.

## Security model: network-only

There is **no application-level auth** on `/query` — no API key, no IAM check,
nothing. Access is controlled entirely by network reachability: the security group
on whatever host runs this service should only allow inbound traffic from the
specific hosts/services that are meant to call it (e.g. mosaic-ai-chat's server),
nothing else, and never the open internet. Since `/query` also accepts arbitrary
SQL, anything that *can* reach this port can run anything against Redshift - the
security group is the only thing standing between "reachable" and "full access," so
keep it tight and review it whenever a new caller needs access.

## Calling it

No client library, no signing, no auth header — it's a plain REST call from
anything that can reach the host:

```
POST http://<gateway-host>:8000/query
{"sql": "SELECT * FROM insurance.policies WHERE policy_id = '123'"}
```

## Local dev

```
pip install -r requirements.txt
uvicorn app.main:app --reload
```

```
curl -X POST localhost:8000/query \
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
Whatever runs it, lock down the security group as described above before pointing
any real caller at it.
