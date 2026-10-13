"""Gateway entrypoint - a plain long-running FastAPI process.

Runs as a pooled-connection service (systemd on an EC2 instance, or an ECS/Fargate
task within the VPC). No application-level auth - protection is network-level only
(security group restricting which hosts can reach this service). See
ARCHITECTURE.md.

Local dev: `uvicorn app.main:app --reload`
Production: `gunicorn -k uvicorn.workers.UvicornWorker app.main:app -w 2 -b 0.0.0.0:8339`
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routes import health, query, sharepoint
from app.sharepoint_client import SharePointError, close_sharepoint_client


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await close_sharepoint_client()


app = FastAPI(
    title="Insurance Data Gateway",
    description="Shared backend: some common service for all microservices",
    version="0.3.0",
    lifespan=lifespan,
)

app.add_exception_handler(SharePointError, sharepoint.sharepoint_error_handler)

app.include_router(health.router)
app.include_router(query.router)
app.include_router(sharepoint.router)
