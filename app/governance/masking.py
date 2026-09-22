"""Field-level masking for PII/PHI in insurance records.

There is no per-caller auth in front of this service (see ARCHITECTURE.md -
protection is network-level: a security group only allowing trusted sources to
reach this host), so masking applies uniformly to every response rather than being
grantable per caller. Applied AFTER the query runs and BEFORE the response leaves
the gateway.
"""
from typing import Any

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


def mask_record(record: dict[str, Any]) -> dict[str, Any]:
    masked = dict(record)
    for field in DEFAULT_SENSITIVE_FIELDS:
        if field in masked:
            masked[field] = _mask_value(masked[field])
    return masked


def _mask_value(value: Any) -> str:
    if value is None:
        return value
    text = str(value)
    if len(text) <= 4:
        return "*" * len(text)
    return "*" * (len(text) - 4) + text[-4:]
