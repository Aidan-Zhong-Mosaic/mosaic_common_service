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
- `/sharepoint/*` — read, write, list, create and delete files and directories in one
  SharePoint folder (see [SharePoint endpoints](#sharepoint-endpoints)).

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
POST http://<gateway-host>:8339/query
{"sql": "SELECT * FROM insurance.policies WHERE policy_id = '123'"}
```

## Local dev

```
pip install -r requirements.txt
uvicorn app.main:app --reload
```

```
curl -X POST localhost:8339/query \
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

## SharePoint endpoints

File operations on one SharePoint folder through Microsoft Graph. Every `path` is
relative to a root folder set in config and cannot leave it (`..` is rejected). Like
`/query`, there is **no application-level auth**: anything that can reach the service
can use them, so keep the security group tight.

| Endpoint | Body / query | What it does |
|---|---|---|
| `GET /sharepoint/read_file` | `?path=` | Returns the file's bytes with its content type. |
| `POST /sharepoint/write_file` | `{path, content, mode, content_encoding}` | `mode` `"w"` creates or overwrites, `"a"` appends (creates if missing), `"x"` creates and fails with 409 if it exists. `content_encoding` is `"utf-8"` (default) or `"base64"` for binary. The parent directory must exist. |
| `POST /sharepoint/create_file` | `{path, content?, content_encoding?}` | Creates a new file (empty by default). 409 if it exists. |
| `POST /sharepoint/append_excel_row` | `{path, table? or sheet?, values? or row?, allow_formulas?}` | Adds one row to an `.xlsx` workbook. Anything that is not a real Excel workbook gets **415**. See [Excel rows](#excel-rows). |
| `POST /sharepoint/delete_file` | `{path}` | Deletes one file. Refuses directories. |
| `GET /sharepoint/list_files` | `?path=&recursive=` | Lists a directory (empty path = the root). |
| `POST /sharepoint/create_directory` | `{path}` | Creates the directory and any missing parents. Safe to repeat. |
| `POST /sharepoint/delete_directory` | `{path}` | Deletes a directory and everything in it. The root folder itself can never be deleted. |

Deletes go to the SharePoint recycle bin. Errors come back as JSON `{"detail": ...}`
with 400 (bad path), 403 (read-only mode), 404, 409 (already exists / changed
meanwhile or locked), 413 (over the size limit), 415 (not an Excel workbook), 502
(Microsoft rejected the call) or 503 (not configured). Every call is written to the
audit log (`app/governance/audit.py`).

```
curl "http://<host>:8339/sharepoint/list_files?path=2026"
curl -X POST http://<host>:8339/sharepoint/write_file \
  -H 'content-type: application/json' \
  -d '{"path": "2026/notes.txt", "content": "hello\n", "mode": "a"}'
```

**Configuration** (environment variables; in Kubernetes the Helm `config.sharepoint*`
values, see `deploy/helm/mosaic-common-service/values.yaml`):

| Variable | |
|---|---|
| `GATEWAY_SHAREPOINT_ROOT_URL` | The root folder, as its browser URL, e.g. `https://contoso.sharepoint.com/sites/Team/Shared Documents/Reports`. The address-bar form with `?id=...` also works. |
| `GATEWAY_SHAREPOINT_TENANT_ID`, `GATEWAY_SHAREPOINT_CLIENT_ID` | The Entra ID app registration the service signs in as. |
| `GATEWAY_SHAREPOINT_CLIENT_SECRET` | Its secret. Keep out of git; in Kubernetes it comes from a Secret (`sharepoint.existingSecret`). |
| `GATEWAY_SHAREPOINT_READ_ONLY` | `true` disables write, create and delete (default `false`). |
| `GATEWAY_SHAREPOINT_MAX_FILE_BYTES` | Largest file read or written (default 50 MiB). |

**Microsoft side:** the app registration needs a Graph *application* permission. The
narrowest is `Sites.Selected`, with a SharePoint admin granting the app the `write`
role (or `read` for a read-only deployment) on this one site; `Sites.ReadWrite.All`
also works but covers every site. This is the same pattern as the chat app's
Interactive Help sync (`docs/interactive-help-sharepoint-integration.md` in
mosaic-ai-chat). Until the grant exists the endpoints return 502 naming the missing
permission, and without the settings above they return 503.

Notes: appends are read-modify-write (Graph has no append), guarded by the file's
ETag so a concurrent change fails with 409 instead of being overwritten. Files over
4 MiB are uploaded in chunks automatically.

### Excel rows

`POST /sharepoint/append_excel_row` edits the workbook in place through Graph's Excel
API, so charts, pivots and formatting are untouched and someone with the file open sees
the row appear. It returns **201** with the row's position.

```
# Recommended: add to an Excel table (Insert > Table). Cells by column name...
curl -X POST http://<host>:8339/sharepoint/append_excel_row -H 'content-type: application/json' \
  -d '{"path": "2026/orders.xlsx", "table": "Orders", "row": {"Name": "Ada", "Amount": 12.5}}'
# ...or in column order
  -d '{"path": "2026/orders.xlsx", "table": "Orders", "values": ["Ada", 12.5, "2026-10-13"]}'
# Without a table: below the last used row of a sheet (the first sheet if omitted)
  -d '{"path": "2026/orders.xlsx", "sheet": "Data", "values": ["Ada", 12.5]}'
```

- **Not an Excel file → 415.** The name must end in `.xlsx`, and the file's first bytes
  must be a real `.xlsx` (so a CSV or old `.xls` renamed to `.xlsx` is refused too).
  `.xls`, `.xlsm` and `.csv` are not supported by Graph's Excel API.
- **Use a table when you can.** Excel adds rows to a table one at a time, so
  simultaneous callers never overwrite each other. The no-table mode picks the next
  free row itself, and two calls at the same moment could pick the same one.
- With a table, `values` needs exactly one value per column (use `null` for an empty
  cell); `row` fills columns by header and leaves the rest empty. A wrong column name
  or count is a 400 that lists the table's real columns.
- **Formulas are refused by default.** Text that starts with `=`, `+`, `-` or `@` would
  be run by Excel as a formula, and these endpoints have no caller auth, so the call
  fails with 400 unless `allow_formulas` is `true`. Send negative numbers as JSON
  numbers, not text.
- A file someone has checked out or is saving gives 409; retry shortly.

## Deploying

Holds a real connection pool, so it runs as a long-lived process, not Lambda — see
`deploy/README.md` for a systemd (EC2) example and the recommended ECS/Fargate path.
Whatever runs it, lock down the security group as described above before pointing
any real caller at it.
