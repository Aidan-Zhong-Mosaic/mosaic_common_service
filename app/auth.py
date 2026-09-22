"""Resolve caller identity from the verified-caller header API Gateway injects.

We do NOT verify SigV4 signatures ourselves - the AWS_IAM authorizer on the API
Gateway HTTP API in front of this service does that. Because this service now runs
as a long-lived process (not Lambda) reached through a VPC Link, the verified
identity is passed down as a request header via an API Gateway integration
parameter mapping (`integration.request.header.x-verified-caller-arn` <-
`$context.authorizer.iam.userArn`), rather than being read out of a Lambda event.
Our job here is just to map that identity to what it's allowed to do.
"""
from fastapi import HTTPException, Request

from app.governance.access_control import get_caller_grants
from app.models import CallerIdentity

VERIFIED_CALLER_HEADER = "x-verified-caller-arn"


def resolve_caller_identity(request: Request) -> CallerIdentity:
    """FastAPI dependency. In production this header is only ever set by API
    Gateway after SigV4 verification - the service should sit behind a VPC Link /
    security group that only accepts traffic from API Gateway, so this header can't
    be spoofed by a caller talking to the service directly.
    """
    iam_arn = request.headers.get(VERIFIED_CALLER_HEADER)

    if not iam_arn:
        raise HTTPException(status_code=401, detail="No verified caller identity present")

    grants = get_caller_grants(iam_arn)
    if grants is None:
        raise HTTPException(status_code=403, detail=f"No access grants configured for {iam_arn}")

    return grants
