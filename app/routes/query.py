"""Single endpoint: run raw SQL against Redshift.

No schema is ever set on the connection (no `search_path`, no default schema) - see
app/redshift_client.py. Every query passed here must fully qualify tables as
`schema.table`, since the connection has no notion of a "current" schema.

Note: this intentionally accepts arbitrary SQL from callers. That's a deliberate
simplification for now - it means access control and PII masking can only be
enforced at the caller level (is this IAM identity allowed to hit the gateway at
all) and generically on known-sensitive column names in the result, not per-table
or per-query. If per-table/column governance becomes a requirement later, that
needs enforcing here (e.g. parsing/allow-listing schemas, or reintroducing named
query templates).
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.concurrency import run_in_threadpool

from app.auth import resolve_caller_identity
from app.governance.audit import log_query
from app.governance.masking import mask_record
from app.models import CallerIdentity, QueryRequest, QueryResponse
from app.redshift_client import RedshiftQueryError, get_redshift_client

router = APIRouter()


@router.post("/query", response_model=QueryResponse)
async def run_query(
    body: QueryRequest,
    identity: CallerIdentity = Depends(resolve_caller_identity),
) -> QueryResponse:
    try:
        rows = await run_in_threadpool(get_redshift_client().execute, body.sql)
    except RedshiftQueryError as exc:
        log_query(
            caller_service=identity.service_name,
            caller_arn=identity.iam_arn,
            sql=body.sql,
            row_count=None,
            status="failed",
        )
        raise HTTPException(status_code=502, detail=f"Redshift query failed: {exc}") from exc

    masked_rows = [mask_record(r, identity) for r in rows]

    log_query(
        caller_service=identity.service_name,
        caller_arn=identity.iam_arn,
        sql=body.sql,
        row_count=len(rows),
        status="ok",
    )

    return QueryResponse(rows=masked_rows, row_count=len(rows))
