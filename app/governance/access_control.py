"""Maps a caller's IAM identity to whether it's allowed to use the gateway at all.

Grants are declarative and reviewed like any access-control change. In production
this would likely be loaded from a config file/table maintained by the platform
team rather than hardcoded, but the shape stays the same.
"""
from app.models import CallerIdentity

# iam_arn -> grants. Replace with a config/DB-backed lookup before production use.
_GRANTS: dict[str, CallerIdentity] = {
    "arn:aws:iam::111111111111:role/mosaic-ai-chat": CallerIdentity(
        service_name="mosaic-ai-chat",
        iam_arn="arn:aws:iam::111111111111:role/mosaic-ai-chat",
        unmasked_fields=set(),  # masked PII/PHI by default
    ),
}


def get_caller_grants(iam_arn: str) -> CallerIdentity | None:
    return _GRANTS.get(iam_arn)
