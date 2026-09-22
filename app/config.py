"""Central configuration for the gateway, loaded from environment variables."""
from functools import lru_cache

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Direct Redshift connection (requires network access to the cluster - VPN/
    # peering/Transit Gateway/PrivateLink depending on environment; see ARCHITECTURE.md)
    redshift_host: str = ""
    redshift_port: int = 5439
    redshift_database: str = "insurance"

    # Path to a local JSON file holding {"username": "...", "password": "..."} -
    # plain username/password auth, not IAM database auth. Never commit this file;
    # see .gitignore and app/redshift_client.py for the expected shape.
    redshift_credentials_file: str = "/etc/mosaic_common_service/redshift-credentials.json"

    # Connection pool
    pool_min_conns: int = 2
    pool_max_conns: int = 10
    query_timeout_seconds: float = 30.0

    # Governance
    audit_log_table: str = "mosaic_common_service-audit-log"

    class Config:
        env_prefix = "GATEWAY_"


@lru_cache
def get_settings() -> Settings:
    return Settings()
