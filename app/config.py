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

    # SharePoint file access via Microsoft Graph (app-only client credentials).
    # `sharepoint_root_url` is the folder every request path is relative to: the
    # browser URL of a folder, e.g.
    #   https://contoso.sharepoint.com/sites/Team/Shared Documents/Reports
    # The endpoints cannot reach anything outside it. See app/sharepoint_client.py.
    sharepoint_root_url: str = ""
    sharepoint_tenant_id: str = ""
    sharepoint_client_id: str = ""
    # Secret: set via the environment (a Kubernetes Secret in the cluster), never
    # in a committed file.
    sharepoint_client_secret: str = ""
    # How /sharepoint/append_excel_row edits the workbook:
    #   "download": download the .xlsx, add the row here, upload it again. Needs only
    #               file access, so it works with a Sites.Selected grant. Re-saving can
    #               drop charts, images and other advanced features.
    #   "graph":    Microsoft's Excel API edits the file in place and keeps everything,
    #               but it refuses apps that only hold Sites.Selected.
    sharepoint_excel_mode: str = "download"
    # Turns off every endpoint that changes SharePoint (write, create, delete).
    sharepoint_read_only: bool = False
    sharepoint_max_file_bytes: int = 50 * 1024 * 1024
    sharepoint_timeout_seconds: float = 30.0
    # login.microsoftonline.com resolves IPv6-only on some of our networks and the
    # containers have no IPv6 route; pins the token request to IPv4 (the chat app
    # does the same). Only affects the token call, not Graph itself.
    sharepoint_token_force_ipv4: bool = True

    # Governance
    audit_log_table: str = "mosaic_common_service-audit-log"

    class Config:
        env_prefix = "GATEWAY_"
        # Local runs read settings (including secrets) from a gitignored .env in the
        # working directory; real environment variables always win over it. In
        # containers there is no .env, so only the environment is used.
        env_file = ".env"
        extra = "ignore"


@lru_cache
def get_settings() -> Settings:
    return Settings()
