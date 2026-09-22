"""Append-only audit logging for every query the gateway runs.

Writes independently of the caller's own logging, so the audit trail exists even if
the calling service's logs are lost or the caller misbehaves. Defaults to structured
stdout logging (captured by CloudWatch/journald); swap in a DynamoDB/Redshift sink
by implementing AuditSink and wiring it in `main.py`.
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
    caller_service: str,
    caller_arn: str,
    sql: str,
    row_count: int | None,
    status: str,
) -> None:
    _sink.write(
        {
            "timestamp": datetime.datetime.utcnow().isoformat() + "Z",
            "caller_service": caller_service,
            "caller_arn": caller_arn,
            "sql": sql,
            "row_count": row_count,
            "status": status,
        }
    )
