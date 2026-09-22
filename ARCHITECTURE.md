# Insurance Data Gateway — Architecture

## Purpose

A single, centrally governed FastAPI backend onto our Redshift insurance data
warehouse, usable by mosaic-ai-chat and any other microservice, regardless of
language — instead of every microservice connecting to Redshift directly.

## Why one backend instead of every service connecting directly?

Redshift is a columnar analytical warehouse, not built for many concurrent
connections. If every microservice (Node, Python, Java, ...) opens its own
connection, we hit connection limits and lose any single place to enforce access
control or audit logging — that logic would have to be reimplemented per service
and will drift. So the actual Redshift access logic lives in one backend; callers
just hit its REST API with a SigV4-signed request using their own IAM role — no
custom client library required, any AWS SDK can sign a plain HTTP request.

## Network connectivity to Redshift (important context, read before deploying)

The Redshift cluster used here sits in a network this account/VPC does not have
direct access to by default. During setup we confirmed:

- Pinging the cluster hostname resolves to a private VPC-endpoint IP
  (`10.18.0.191`, behind `vpce-0a1648f516c8c4d20`) — DNS resolution alone does not
  mean a route exists.
- Direct TCP to port 5439 fails (silent timeout, not "connection refused") without
  the network path up — consistent with a missing route/security-group rule, not a
  listening-service problem.
- The only currently-available path in is an **AWS Client VPN** endpoint
  (`cvpn-endpoint-06dd8e32baf5b636c...clientvpn.eu-west-1.amazonaws.com`), a
  remote-access VPN product meant for individual devices, authenticated via a
  personal client certificate. Bringing that tunnel up on the server EC2
  (`openvpn --config <profile>.ovpn --daemon`) confirmed port 5439 becomes reachable.

**Using a personal Client VPN certificate on a production server is a stopgap, not
the end state.** It should be treated as a real open item:

- Ask whoever administers the Client VPN endpoint for a **dedicated client
  certificate for this service** (its own access group), not a human's laptop
  profile, so revoking/rotating one doesn't affect the other.
- Ask them to **narrow the Client VPN Route Table entry** to the specific CIDR/host
  the Redshift cluster's endpoint lives on, rather than the broad set of internal
  CIDRs (`10.0.0.0/22`, `10.4.0.0/22`, etc.) currently pushed to every connected
  client — Client VPN is split-tunnel by default, so a narrower route means this
  server's tunnel literally cannot reach anything else on that network.
- Longer term, prefer removing the VPN dependency entirely: a VPC peering
  connection, Transit Gateway attachment, or a PrivateLink interface endpoint placed
  directly in this service's own VPC pointing at the Redshift endpoint service.
  Any of these avoids depending on an OpenVPN process staying up in production.

Until one of those is in place, whatever compute runs the gateway needs the VPN
tunnel active (see `deploy/` for a systemd example that starts the gateway after
the VPN unit).

## Components

```
   mosaic-ai-chat   ───▶  API Gateway (HTTP API, AWS_IAM authorizer)
   other service    ───▶            │  SigV4-verified
                                     │  (caller ARN forwarded as
                                     │   x-verified-caller-arn header)
                                     ▼
                          VPC Link ──▶ ALB/NLB ──▶ Insurance Data Gateway
                                                    (long-running FastAPI process:
                                                     EC2/systemd or ECS/Fargate)
                                                      ┌─────────────────────┐
                                                      │ auth/identity        │
                                                      │ caller allow-list     │
                                                      │ PII/PHI masking       │
                                                      │ audit logging         │
                                                      └─────────┬───────────┘
                                                                ▼
                                                    ┌──────────────────────┐
                                                    │ psycopg2 pooled conn  │
                                                    │ (no schema set - every│
                                                    │  query is schema.table)│
                                                    │ over VPN/peering/     │
                                                    │ PrivateLink to Redshift│
                                                    └──────────────────────┘
```

- **API Gateway (HTTP API) with an AWS_IAM authorizer** — every caller signs
  requests with SigV4 using its own IAM role. API Gateway verifies the signature
  before the request reaches our code. The verified caller identity is forwarded to
  the backend as a request header (`x-verified-caller-arn`, via an integration
  request parameter mapping from `$context.authorizer.iam.userArn`), since the
  gateway is no longer a Lambda that receives the raw event.

- **Gateway backend (Python, FastAPI)** — runs as a **long-lived process**
  (systemd on EC2, or an ECS/Fargate task), not Lambda, because it holds a real
  connection pool (`app/redshift_client.py`). Redshift's connection limits mean we
  want a small, stable pool rather than one connection per invocation.

- **Single endpoint (`POST /query`)** — takes a raw SQL string and runs it as-is
  against the pool. The connection never sets a schema/`search_path`, so every
  query must fully qualify `schema.table` itself.

- **Governance layer** (inside the gateway, not optional/bolt-on, but narrower than
  a template-based design would allow - see below):
  - *Access control*: only gates whether a caller's IAM identity is allowed to hit
    `/query` at all (`app/governance/access_control.py`) - it cannot restrict which
    tables/schemas a permitted caller queries, since the SQL is arbitrary.
  - *Masking*: known PII/PHI field names (SSN, DOB, policyholder name, etc.) are
    masked wherever they appear in a result, by column name, regardless of the
    query that produced them (`app/governance/masking.py`).
  - *Audit logging*: every request logs caller identity, the full SQL text, row
    count, and status, independent of the caller's own logs
    (`app/governance/audit.py`).

- **Callers** — no SDK to install. Any service signs a plain HTTPS request with
  SigV4 using its own IAM role (every language's AWS SDK can do this) and calls
  `POST /query` directly with `{"sql": "..."}`.

## Request flow

Caller → SigV4-signed `POST /query` → API Gateway verifies signature, forwards
caller ARN → gateway checks the caller is on the allow-list → SQL runs against the
pooled connection (bounded by a `statement_timeout`,
`GATEWAY_QUERY_TIMEOUT_SECONDS`, so a runaway query can't hold a pool slot
indefinitely; executed via FastAPI's threadpool so it doesn't block other
requests) → result rows are masked by column name → audit logged → returned.

## Why one raw-SQL endpoint instead of named query templates

This is a deliberate simplification, worth being explicit about: accepting
arbitrary SQL means access control and masking can only work at the caller level
(allowed to use the gateway at all) and generically on column names, not per
table/schema/query. An earlier design used named, parameterized query templates
reviewed like schema changes specifically to get finer-grained governance; that
tradeoff was dropped here in favor of a much simpler surface. If per-table/column
enforcement becomes a real requirement later (e.g. once more than one team is
calling this and they shouldn't all see the same tables), that's the point to
revisit - either by validating/allow-listing referenced schemas per caller, or by
reintroducing named templates for the callers that need tighter scoping.

## Repo layout

```
insurance-data-gateway/
  ARCHITECTURE.md
  README.md
  app/
    main.py
    config.py
    auth.py
    redshift_client.py
    models.py
    governance/
      access_control.py
      masking.py
      audit.py
    routes/
      health.py
      query.py
  tests/
  deploy/                  # systemd unit + ECS/API Gateway notes
  policies/
    example-consumer-iam-policy.json
  requirements.txt
  Dockerfile
```

## Open decisions for the dev team

- **Network path to Redshift** — replace the personal Client VPN certificate with a
  dedicated server credential (scoped route + authorization rule), or better, VPC
  peering / Transit Gateway / a PrivateLink endpoint in the gateway's own VPC.
- Whether per-table/schema access control is needed once more callers exist (see
  "Why one raw-SQL endpoint" above) - the current design trusts every allow-listed
  caller with the full schema.
- Which fields count as PII/PHI for masking, and who owns that classification?
- Audit log destination and retention (compliance will likely require a retention
  period) — currently logs to stdout only.
- Pool sizing (`GATEWAY_POOL_MIN_CONNS`/`GATEWAY_POOL_MAX_CONNS`) relative to
  Redshift's connection limit and expected concurrent request volume.
