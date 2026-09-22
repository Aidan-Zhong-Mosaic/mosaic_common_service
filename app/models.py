"""Shared request/response models."""
from typing import Any

from pydantic import BaseModel, Field


class QueryRequest(BaseModel):
    sql: str = Field(..., description="Raw SQL to run. Must fully qualify schema.table - no default schema/search_path is set on the connection.")


class QueryResponse(BaseModel):
    rows: list[dict[str, Any]]
    row_count: int
