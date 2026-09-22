"""Single endpoint: run raw SQL against Redshift.

No schema is ever set on the connection (no `search_path`, no default schema) - see
app/redshift_client.py. Every query passed here must fully qualify tables as
`schema.table`, since the connection has no notion of a "current" schema.

There is no application-level auth on this endpoint - protection is network-level
only (a security group that restricts which hosts can reach this service at all;
see ARCHITECTURE.md). Combined with accepting arbitrary SQL, that means anything
that can reach this port can run anything against Redshift. Keep the security group
tight.
"""
from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from app.governance.audit import log_query
from app.governance.masking import mask_record
from app.models import QueryRequest, QueryResponse
from app.redshift_client import RedshiftQueryError, get_redshift_client

router = APIRouter()


@router.post("/query", response_model=QueryResponse)
async def run_query(body: QueryRequest, request: Request) -> QueryResponse:
    client_host = request.client.host if request.client else None

    try:
        rows = await run_in_threadpool(get_redshift_client().execute, body.sql)
    except RedshiftQueryError as exc:
        log_query(client_host=client_host, sql=body.sql, row_count=None, status="failed")
        raise HTTPException(status_code=502, detail=f"Redshift query failed: {exc}") from exc

    masked_rows = [mask_record(r) for r in rows]

    log_query(client_host=client_host, sql=body.sql, row_count=len(rows), status="ok")

    return QueryResponse(rows=masked_rows, row_count=len(rows))
