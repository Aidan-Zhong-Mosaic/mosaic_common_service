"""Field-level masking for PII/PHI in insurance records.

Masking is applied AFTER access control (the caller is allowed to see the row) and
BEFORE the response leaves the gateway, so no unmasked value ever crosses the network
boundary to a caller without explicit grant.
"""
from typing import Any

from app.models import CallerIdentity

# Fields considered PII/PHI by default. This classification should be owned jointly
# by the data/platform team and compliance, and kept in sync with the schema.
DEFAULT_SENSITIVE_FIELDS = {
    "ssn",
    "date_of_birth",
    "policyholder_name",
    "address",
    "phone_number",
    "email",
}


def mask_record(record: dict[str, Any], identity: CallerIdentity) -> dict[str, Any]:
    masked = dict(record)
    for field in DEFAULT_SENSITIVE_FIELDS:
        if field in masked and field not in identity.unmasked_fields:
            masked[field] = _mask_value(masked[field])
    return masked


def _mask_value(value: Any) -> str:
    if value is None:
        return value
    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * (len(text) - 4) + text[-4:]
