"""Pooled, direct psycopg2 connection to Redshift.

Requires actual network connectivity to the cluster (VPN / VPC peering /
Transit Gateway / PrivateLink depending on environment - see ARCHITECTURE.md's
"Network connectivity" section). Because this holds a real connection pool, this
service needs to run as a long-lived process (systemd on an EC2 instance, or an
ECS/Fargate task within the VPC) rather than a short-lived Lambda invocation -
pools don't survive between separate Lambda invocations reliably, and Redshift
has meaningfully lower connection limits than an OLTP database, so we want a
small number of long-lived pooled connections, not one per invocation.
"""
import json
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import psycopg2
import psycopg2.extras
from psycopg2 import pool as pg_pool

from app.config import get_settings


class RedshiftQueryError(Exception):
    pass


class RedshiftCredentialsError(Exception):
    pass


def _load_credentials(credentials_file: str) -> tuple[str, str]:
    """Reads plain username/password DB credentials from a local JSON file, e.g.:

        {"username": "svc_insurance_data_gateway", "password": "..."}

    This is NOT IAM database auth - Redshift here is configured for regular
    username/password auth, so there's no IAM role to assume for the DB connection
    itself (separate from the AWS_IAM authorizer in front of the HTTP API, which is
    unrelated to how we talk to Redshift). Keep this file out of git (see
    .gitignore) and restrict its permissions - we check for that below and refuse
    to start if the file is group/world-readable.
    """
    path = Path(credentials_file)
    if not path.is_file():
        raise RedshiftCredentialsError(
            f"Redshift credentials file not found at '{credentials_file}' "
            "(set GATEWAY_REDSHIFT_CREDENTIALS_FILE if it lives elsewhere)."
        )

    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise RedshiftCredentialsError(
            f"Refusing to read '{credentials_file}': it is readable by group/other. "
            f"Run `chmod 600 {credentials_file}`."
        )

    data = json.loads(path.read_text())
    try:
        return data["username"], data["password"]
    except KeyError as exc:
        raise RedshiftCredentialsError(
            f"'{credentials_file}' must contain both \"username\" and \"password\" keys."
        ) from exc


class RedshiftClient:
    """Module-level singleton (see `get_redshift_client`). Holds one connection
    pool for the life of the process.
    """

    def __init__(self) -> None:
        settings = get_settings()
        username, password = _load_credentials(settings.redshift_credentials_file)

        # Deliberately does not set `search_path` / any default schema. Every query
        # run through this client must fully qualify `schema.table` itself - see
        # app/routes/query.py.
        self._pool = pg_pool.ThreadedConnectionPool(
            minconn=settings.pool_min_conns,
            maxconn=settings.pool_max_conns,
            host=settings.redshift_host,
            port=settings.redshift_port,
            dbname=settings.redshift_database,
            user=username,
            password=password,
            connect_timeout=10,
            options=f"-c statement_timeout={int(settings.query_timeout_seconds * 1000)}",
        )

    @contextmanager
    def _connection(self):
        conn = self._pool.getconn()
        try:
            yield conn
        finally:
            self._pool.putconn(conn)

    def execute(self, sql: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        """Runs a query against the pool and returns rows as dicts.

        The connection has no schema/search_path set (see __init__) - every query
        must fully qualify tables as `schema.table` itself.

        `params` is optional and only for internal callers that want psycopg2's
        named-parameter substitution (`%(param)s`). When it's omitted (the case for
        the public /query endpoint, which passes raw caller SQL as-is), we do NOT
        pass a params arg to psycopg2 at all - passing even an empty dict makes
        psycopg2 parse `%` as a placeholder marker, which would break any caller
        SQL containing a literal `%` (e.g. `LIKE '%foo%'`).
        """
        with self._connection() as conn:
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    if params:
                        cur.execute(sql, params)
                    else:
                        cur.execute(sql)
                    if cur.description is None:
                        conn.commit()
                        return []
                    rows = [dict(row) for row in cur.fetchall()]
                    conn.commit()
                    return rows
            except Exception as exc:
                conn.rollback()
                raise RedshiftQueryError(str(exc)) from exc

    def close(self) -> None:
        self._pool.closeall()


_client: RedshiftClient | None = None


def get_redshift_client() -> RedshiftClient:
    global _client
    if _client is None:
        _client = RedshiftClient()
    return _client
