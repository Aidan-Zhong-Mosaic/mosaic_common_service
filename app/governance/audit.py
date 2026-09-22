"""Append-only audit logging for every query the gateway runs.

There's no per-caller identity anymore (see ARCHITECTURE.md - this service relies
on network-level protection, not application auth), so we log the request's source
IP instead of a caller identity. Writes independently of the caller's own logging,
so the audit trail exists even if the calling service's logs are lost. Defaults to
structured stdout logging (captured by CloudWatch/journald); swap in a
DynamoDB/Redshift sink by implementing AuditSink and wiring it in `main.py`.
"""
import datetime
import json
import logging
from typing import Any, Protocol

logger = logging.getLogger("audit")


class AuditSink(Protocol):
    def write(self, event: dict[str, Any]) -> None: ...


class StdoutAuditSink:
    def write(self, event: dict[str, Any]) -> None:
        logger.info(json.dumps(event, default=str))


_sink: AuditSink = StdoutAuditSink()


def set_audit_sink(sink: AuditSink) -> None:
    global _sink
    _sink = sink


def log_query(
    *,
    client_host: str | None,
    sql: str,
    row_count: int | None,
    status: str,
) -> None:
    _sink.write(
        {
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "client_host": client_host,
            "sql": sql,
            "row_count": row_count,
            "status": status,
        }
    )
