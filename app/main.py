"""Gateway entrypoint - a plain long-running FastAPI process.

Runs as a pooled-connection service (systemd on an EC2 instance, or an ECS/Fargate
task within the VPC), reached through an API Gateway HTTP API (AWS_IAM authorizer)
+ VPC Link. See ARCHITECTURE.md.

Local dev: `uvicorn app.main:app --reload`
Production: `gunicorn -k uvicorn.workers.UvicornWorker app.main:app -w 2 -b 0.0.0.0:8000`
"""
from fastapi import FastAPI

from app.routes import health, query

app = FastAPI(
    title="Insurance Data Gateway",
    description="Single governed entrypoint for running SQL against Redshift.",
    version="0.3.0",
)

app.include_router(health.router)
app.include_router(query.router)
