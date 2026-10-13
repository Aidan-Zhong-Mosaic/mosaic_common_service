"""Shared request/response models."""
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class QueryRequest(BaseModel):
    sql: str = Field(..., description="Raw SQL to run. Must fully qualify schema.table - no default schema/search_path is set on the connection.")


class QueryResponse(BaseModel):
    rows: list[dict[str, Any]]
    row_count: int


# --- SharePoint -------------------------------------------------------------
# Every `path` is relative to the configured root folder (GATEWAY_SHAREPOINT_ROOT_URL).

class SharePointPathRequest(BaseModel):
    path: str = Field(..., min_length=1, description="Path relative to the SharePoint root folder, e.g. 'reports/2026/q3.xlsx'.")


class SharePointWriteRequest(BaseModel):
    path: str = Field(..., min_length=1, description="File path relative to the root folder. The parent directory must already exist.")
    content: str = Field(..., description="The data to write. Text by default; set content_encoding to 'base64' for binary files.")
    mode: Literal["w", "a", "x"] = Field(
        "w",
        description="'w' create or overwrite, 'a' append (creates the file if missing), 'x' create and fail if it already exists.",
    )
    content_encoding: Literal["utf-8", "base64"] = "utf-8"


class SharePointCreateFileRequest(BaseModel):
    path: str = Field(..., min_length=1, description="File path relative to the root folder. Fails with 409 if the file already exists.")
    content: str = Field("", description="Optional initial content. Empty by default.")
    content_encoding: Literal["utf-8", "base64"] = "utf-8"


class SharePointItem(BaseModel):
    name: str
    path: str = Field(..., description="Path relative to the root folder.")
    type: Literal["file", "directory"]
    size: int | None = None
    last_modified: str | None = None
    web_url: str | None = None
    etag: str | None = None


class SharePointListResponse(BaseModel):
    path: str
    items: list[SharePointItem]
    count: int
    truncated: bool = Field(False, description="True if the listing stopped at the server's item limit.")


CellValue = str | int | float | bool | None


class SharePointExcelRowRequest(BaseModel):
    path: str = Field(..., min_length=1, description="Path of the .xlsx workbook relative to the root folder. Any other file type is rejected with 415.")
    values: list[CellValue] | None = Field(
        None,
        description="The new row's cells in column order. For a table there must be exactly one value per column. Use null for an empty cell.",
    )
    row: dict[str, CellValue] | None = Field(
        None,
        description="Alternative to values, table mode only: cells by column header, e.g. {\"Name\": \"Ada\", \"Amount\": 12.5}. Columns left out stay empty.",
    )
    table: str | None = Field(
        None,
        description="Name of an Excel table (Insert > Table) to add the row to. Recommended: simultaneous callers never overwrite each other.",
    )
    sheet: str | None = Field(
        None,
        description="Without a table: the worksheet to add the row to, below its last used row. Defaults to the first worksheet.",
    )
    allow_formulas: bool = Field(
        False,
        description="Text starting with = + - @ is refused by default because Excel would run it as a formula. Set true to write formulas on purpose.",
    )

    @model_validator(mode="after")
    def _check_shape(self) -> "SharePointExcelRowRequest":
        if (self.values is None) == (self.row is None):
            raise ValueError("send exactly one of 'values' or 'row'")
        if self.row is not None and not self.table:
            raise ValueError("'row' (cells by column name) needs a 'table'; without a table, send 'values'")
        if self.table and self.sheet:
            raise ValueError("send 'table' or 'sheet', not both")
        return self


class SharePointExcelRowResponse(BaseModel):
    path: str
    mode: Literal["table", "sheet"]
    table: str | None = None
    sheet: str | None = None
    row_number: int | None = Field(
        None,
        description="1-based: the row's position among the table's data rows (table mode), or the Excel row number (sheet mode).",
    )
    address: str | None = Field(None, description="Sheet mode only: the cells written, e.g. 'A11:C11'.")


class SharePointCreateDirectoryResponse(BaseModel):
    path: str
    created: bool = Field(..., description="False if the directory already existed.")


class SharePointDeleteResponse(BaseModel):
    path: str
    deleted: bool = True
