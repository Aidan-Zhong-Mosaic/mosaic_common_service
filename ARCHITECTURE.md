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
just hit its plain REST API — no custom client library, no signing, no auth header.
Access is controlled at the network level instead (see "Security model" below), not
per-request application auth.

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
   mosaic-ai-chat   ───▶  (security group: only trusted hosts admitted)
   other service    ───▶            │
                                     ▼
                          Insurance Data Gateway
                          (long-running FastAPI process: EC2/systemd or ECS/Fargate)
                            ┌─────────────────────┐
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

- **Gateway backend (Python, FastAPI)** — runs as a **long-lived process**
  (systemd on EC2, or an ECS/Fargate task), not Lambda, because it holds a real
  connection pool (`app/redshift_client.py`). Redshift's connection limits mean we
  want a small, stable pool rather than one connection per invocation.

- **Single endpoint (`POST /query`)** — takes a raw SQL string and runs it as-is
  against the pool. The connection never sets a schema/`search_path`, so every
  query must fully qualify `schema.table` itself.

- **Security model: network-only, no application auth.** There's no API key,
  no IAM check, nothing at the application layer - a security group on whatever
  host runs this is the only thing controlling who can reach `/query` at all. This
  was a deliberate choice (see "Why no application-level auth" below), not an
  oversight - it needs to be kept in mind when deciding what else gets network
  access to this host.

- **Governance layer that remains** (inside the gateway):
  - *Masking*: known PII/PHI field names (SSN, DOB, policyholder name, etc.) are
    masked wherever they appear in a result, by column name, regardless of the
    query that produced them or who's calling - there's no per-caller grant to be
    more permissive (`app/governance/masking.py`).
  - *Audit logging*: every request logs the source IP, the full SQL text, row
    count, and status, independent of the caller's own logs
    (`app/governance/audit.py`).

- **Callers** — no SDK, no signing, no headers. Any process that can reach the host
  makes a plain HTTP `POST /query` with `{"sql": "..."}`.

## Request flow

Caller → plain `POST /query` (reaches the gateway only if the security group
admits that source) → SQL runs against the pooled connection (bounded by a
`statement_timeout`, `GATEWAY_QUERY_TIMEOUT_SECONDS`, so a runaway query can't hold
a pool slot indefinitely; executed via FastAPI's threadpool so it doesn't block
other requests) → result rows are masked by column name → audit logged → returned.

## Why no application-level auth

This was a deliberate simplification, not an oversight, worth being explicit about:
there is currently no IAM/API-key/token check on `/query`, so the entire security
boundary is the network (security group). Combined with `/query` accepting
arbitrary SQL (see below), anything that can reach the port has full,
unauthenticated access to run any query against Redshift. An earlier design used an
API-Gateway-verified IAM identity per caller specifically to get application-level
access control; that was dropped in favor of relying purely on network placement.
If this ever needs to be exposed beyond a small set of trusted internal hosts (more
callers, a less trusted network, compliance requirements), application-level auth
should be revisited before that happens - reintroducing either a shared secret/API
key or the previous IAM-based design.

## Why one raw-SQL endpoint instead of named query templates

Also deliberate: accepting arbitrary SQL means masking can only work generically on
column names, never per table/schema/query, and (per above) there's no way to
restrict which tables a caller can touch short of the network layer. An earlier
design used named, parameterized query templates reviewed like schema changes
specifically to get finer-grained governance; that tradeoff was dropped here in
favor of a much simpler surface. If per-table/column enforcement becomes a real
requirement later (e.g. once more than one team is calling this and they shouldn't
all see the same tables), that's the point to revisit - either by
validating/allow-listing referenced schemas, or by reintroducing named templates.

## Repo layout

```
insurance-data-gateway/
  ARCHITECTURE.md
  README.md
  app/
    main.py
    config.py
    redshift_client.py
    models.py
    governance/
      masking.py
      audit.py
    routes/
      health.py
      query.py
  tests/
  deploy/                  # systemd unit + ECS notes
  requirements.txt
  Dockerfile
```

## Open decisions for the dev team

- **Network path to Redshift** — replace the personal Client VPN certificate with a
  dedicated server credential (scoped route + authorization rule), or better, VPC
  peering / Transit Gateway / a PrivateLink endpoint in the gateway's own VPC.
- **Security group scope for this host** — needs to be defined and kept tight (see
  "Why no application-level auth" above) since it's the only access control that
  exists right now.
- Whether/when application-level auth and per-table access control need to come
  back (see the two "Why" sections above) - revisit if more callers, a less
  trusted network, or compliance requirements show up.
- Which fields count as PII/PHI for masking, and who owns that classification?
- Audit log destination and retention (compliance will likely require a retention
  period) — currently logs to stdout only.
- Pool sizing (`GATEWAY_POOL_MIN_CONNS`/`GATEWAY_POOL_MAX_CONNS`) relative to
  Redshift's connection limit and expected concurrent request volume.
