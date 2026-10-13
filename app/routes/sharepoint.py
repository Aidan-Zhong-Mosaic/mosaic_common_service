"""File operations on a SharePoint folder, through Microsoft Graph.

Every `path` is relative to the root folder set in GATEWAY_SHAREPOINT_ROOT_URL and
cannot leave it. Like /query, these endpoints have no application-level auth:
protection is network-level only, so anything that can reach this service can read
and, unless GATEWAY_SHAREPOINT_READ_ONLY=true, change or delete files in that
folder. Deletes go to the SharePoint recycle bin.
"""
import base64
import binascii
import mimetypes
from contextlib import asynccontextmanager
from dataclasses import asdict
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.governance.audit import log_sharepoint
from app.models import (
    SharePointCreateDirectoryResponse,
    SharePointCreateFileRequest,
    SharePointDeleteResponse,
    SharePointExcelRowRequest,
    SharePointExcelRowResponse,
    SharePointItem,
    SharePointListResponse,
    SharePointPathRequest,
    SharePointWriteRequest,
)
from app.sharepoint_client import (
    BadRequestError,
    ReadOnlyError,
    SharePointClient,
    SharePointError,
    get_sharepoint_client,
    split_path,
)

router = APIRouter(prefix="/sharepoint", tags=["sharepoint"])


async def sharepoint_error_handler(request: Request, exc: SharePointError) -> JSONResponse:
    """Register on the app (see main.py) so every SharePoint failure comes back as
    a JSON error with a useful message instead of a blank 500."""
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


def writes_allowed() -> None:
    if get_settings().sharepoint_read_only:
        raise ReadOnlyError("SharePoint changes are disabled on this deployment (GATEWAY_SHAREPOINT_READ_ONLY=true).")


@asynccontextmanager
async def _audited(request: Request, operation: str, path: str):
    client_host = request.client.host if request.client else None
    try:
        yield
    except Exception:
        log_sharepoint(client_host=client_host, operation=operation, path=path, status="failed")
        raise
    log_sharepoint(client_host=client_host, operation=operation, path=path, status="ok")


def _decode(content: str, content_encoding: str) -> bytes:
    if content_encoding == "base64":
        try:
            return base64.b64decode(content, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BadRequestError("content is not valid base64.") from exc
    return content.encode("utf-8")


@router.get("/read_file")
async def read_file(
    request: Request,
    path: str = Query(..., min_length=1, description="File path relative to the root folder."),
    client: SharePointClient = Depends(get_sharepoint_client),
) -> Response:
    """Returns the file's bytes with its content type."""
    async with _audited(request, "read_file", path):
        data, mime, entry = await client.read_file(split_path(path))
    media_type = mime or mimetypes.guess_type(entry.name)[0] or "application/octet-stream"
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(entry.name)}",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/write_file", response_model=SharePointItem, dependencies=[Depends(writes_allowed)])
async def write_file(
    body: SharePointWriteRequest,
    request: Request,
    client: SharePointClient = Depends(get_sharepoint_client),
) -> SharePointItem:
    """Writes to a file: mode 'w' overwrites, 'a' appends, 'x' fails if it exists."""
    async with _audited(request, f"write_file:{body.mode}", body.path):
        entry = await client.write_file(split_path(body.path), _decode(body.content, body.content_encoding), body.mode)
    return SharePointItem(**asdict(entry))


@router.post("/create_file", response_model=SharePointItem, status_code=201, dependencies=[Depends(writes_allowed)])
async def create_file(
    body: SharePointCreateFileRequest,
    request: Request,
    client: SharePointClient = Depends(get_sharepoint_client),
) -> SharePointItem:
    """Creates a new file (empty unless content is given). 409 if it already exists."""
    async with _audited(request, "create_file", body.path):
        entry = await client.write_file(split_path(body.path), _decode(body.content, body.content_encoding), "x")
    return SharePointItem(**asdict(entry))


@router.post("/append_excel_row", response_model=SharePointExcelRowResponse, status_code=201, dependencies=[Depends(writes_allowed)])
async def append_excel_row(
    body: SharePointExcelRowRequest,
    request: Request,
    client: SharePointClient = Depends(get_sharepoint_client),
) -> SharePointExcelRowResponse:
    """Adds one row to an Excel workbook in SharePoint.

    Pass `table` to add to the end of an Excel table (safe for simultaneous callers),
    or omit it to add below the last used row of a sheet. A file that is not an
    .xlsx workbook is refused with 415.
    """
    async with _audited(request, "append_excel_row", body.path):
        result = await client.append_excel_row(
            split_path(body.path),
            values=body.values,
            row=body.row,
            table=body.table,
            sheet=body.sheet,
            allow_formulas=body.allow_formulas,
        )
    return SharePointExcelRowResponse(path=body.path, **result)


@router.post("/delete_file", response_model=SharePointDeleteResponse, dependencies=[Depends(writes_allowed)])
async def delete_file(
    body: SharePointPathRequest,
    request: Request,
    client: SharePointClient = Depends(get_sharepoint_client),
) -> SharePointDeleteResponse:
    """Deletes one file (to the recycle bin). Refuses directories."""
    async with _audited(request, "delete_file", body.path):
        await client.delete_file(split_path(body.path))
    return SharePointDeleteResponse(path=body.path)


@router.get("/list_files", response_model=SharePointListResponse)
async def list_files(
    request: Request,
    path: str = Query("", description="Directory path relative to the root folder. Empty lists the root."),
    recursive: bool = Query(False, description="Also list everything in subdirectories."),
    client: SharePointClient = Depends(get_sharepoint_client),
) -> SharePointListResponse:
    async with _audited(request, "list_files", path):
        entries, truncated = await client.list_entries(split_path(path), recursive=recursive)
    items = [SharePointItem(**asdict(e)) for e in entries]
    return SharePointListResponse(path=path, items=items, count=len(items), truncated=truncated)


@router.post("/create_directory", response_model=SharePointCreateDirectoryResponse, dependencies=[Depends(writes_allowed)])
async def create_directory(
    body: SharePointPathRequest,
    request: Request,
    client: SharePointClient = Depends(get_sharepoint_client),
) -> SharePointCreateDirectoryResponse:
    """Creates the directory and any missing parent directories. Safe to repeat."""
    async with _audited(request, "create_directory", body.path):
        created = await client.create_directory(split_path(body.path))
    return SharePointCreateDirectoryResponse(path=body.path, created=created)


@router.post("/delete_directory", response_model=SharePointDeleteResponse, dependencies=[Depends(writes_allowed)])
async def delete_directory(
    body: SharePointPathRequest,
    request: Request,
    client: SharePointClient = Depends(get_sharepoint_client),
) -> SharePointDeleteResponse:
    """Deletes a directory and everything inside it (to the recycle bin). The root folder itself cannot be deleted."""
    async with _audited(request, "delete_directory", body.path):
        await client.delete_directory(split_path(body.path))
    return SharePointDeleteResponse(path=body.path)
