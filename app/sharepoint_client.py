"""SharePoint file access through Microsoft Graph.

Every path a caller passes is relative to one configured root folder
(GATEWAY_SHAREPOINT_ROOT_URL) and is validated so it cannot climb out of it.

Auth is app-only (client credentials): the service signs in as its own Entra ID
app registration, not as the calling user, so it can reach whatever that app was
granted. The recommended grant is the Graph application permission `Sites.Selected`
with the write role on this one site (a SharePoint admin applies the role per
site). `Sites.ReadWrite.All` also works but covers every site; a read-only
deployment only needs the read role plus GATEWAY_SHAREPOINT_READ_ONLY=true.

The site and document library are located the way the chat app's Interactive Help
sync does it (`/sites/{host}:{path}`), which works under `Sites.Selected`.

Deleted files and folders go to the SharePoint recycle bin, so a mistake can be
undone from there.
"""
import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from app.config import Settings, get_settings

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"

# Graph's simple upload takes up to 4 MiB; anything larger needs an upload session.
SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
# Chunks for an upload session must be a multiple of 320 KiB; this is 32 of them.
UPLOAD_CHUNK_BYTES = 10 * 1024 * 1024

LIST_PAGE_SIZE = 200
LIST_MAX_ITEMS = 5000

RETRY_STATUSES = {429, 503, 504}
MAX_RETRIES = 3
MAX_RETRY_WAIT_SECONDS = 10.0

# Characters SharePoint does not allow in a file or folder name.
_FORBIDDEN_NAME_CHARS = set('"*:<>?\\|')


class SharePointError(Exception):
    """Base class. `status_code` is what the API answers with."""

    status_code = 502


class SharePointConfigError(SharePointError):
    status_code = 503


class BadRequestError(SharePointError):
    status_code = 400


class InvalidPathError(BadRequestError):
    pass


class ReadOnlyError(SharePointError):
    status_code = 403


class NotFoundError(SharePointError):
    status_code = 404


class ConflictError(SharePointError):
    status_code = 409


class TooLargeError(SharePointError):
    status_code = 413


class UpstreamError(SharePointError):
    status_code = 502


class NotExcelError(SharePointError):
    """The file is not an .xlsx workbook, so the Excel endpoints cannot work on it."""

    status_code = 415


@dataclass(frozen=True)
class Entry:
    name: str
    path: str
    type: str  # "file" or "directory"
    size: int | None
    last_modified: str | None
    web_url: str | None
    etag: str | None


@dataclass(frozen=True)
class RootLocation:
    hostname: str
    site_path: str  # "" for the tenant's root site, otherwise "/sites/Name"
    library: str | None  # URL name of the document library, e.g. "Shared Documents"
    folder: list[str]  # folder segments inside that library


def split_path(path: str) -> list[str]:
    """Validates a caller path (relative to the root) and returns its segments.

    Leading, trailing and doubled slashes are ignored, so "/a//b/" is ["a", "b"].
    ".." is rejected outright, which is what keeps every request inside the root.
    """
    segments: list[str] = []
    for segment in path.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise InvalidPathError("'..' is not allowed in a path.")
        if any(ord(ch) < 32 or ch in _FORBIDDEN_NAME_CHARS for ch in segment):
            raise InvalidPathError(
                f"'{segment}' contains a character SharePoint does not allow "
                '(control characters or any of " * : < > ? \\ |).'
            )
        if segment != segment.strip() or segment.endswith("."):
            raise InvalidPathError(
                f"'{segment}' cannot start or end with a space, or end with a dot."
            )
        if len(segment) > 255:
            raise InvalidPathError("A file or folder name is limited to 255 characters.")
        segments.append(segment)
    return segments


def encode_path(segments: list[str]) -> str:
    return "/".join(quote(segment, safe="") for segment in segments)


def column_letter(index: int) -> str:
    """0 -> A, 25 -> Z, 26 -> AA, 701 -> ZZ, 702 -> AAA."""
    letters = ""
    number = index + 1
    while number:
        number, remainder = divmod(number - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


# Excel reads a typed value that starts with one of these as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def prepare_cells(values: list[Any], *, allow_formulas: bool) -> list[Any]:
    """Turns None into an empty cell and refuses text Excel would run as a formula.

    The endpoints have no caller auth, and these values often come from user input,
    so text like "=HYPERLINK(...)" must not silently become a live formula in a
    shared workbook. Real negative numbers are fine: send them as JSON numbers, not text.
    """
    cells: list[Any] = []
    for value in values:
        if value is None:
            cells.append("")
            continue
        if isinstance(value, str) and not allow_formulas and value.startswith(_FORMULA_PREFIXES):
            raise BadRequestError(
                f"The text '{value[:30]}' starts with '{value[0]}', which Excel would treat as a formula. "
                "Send numbers as JSON numbers, or set allow_formulas to true to write a formula on purpose."
            )
        cells.append(value)
    return cells


def parse_root_url(url: str) -> RootLocation:
    """Breaks the configured root folder URL into site, library and folder.

    Accepts a plain folder URL
        https://contoso.sharepoint.com/sites/Team/Shared Documents/Reports
    and the address-bar form SharePoint shows after you open a folder
        https://contoso.sharepoint.com/sites/Team/Shared%20Documents/Forms/AllItems.aspx?id=%2Fsites%2FTeam%2FShared%20Documents%2FReports
    """
    url = url.strip()
    if not url:
        raise SharePointConfigError("GATEWAY_SHAREPOINT_ROOT_URL is not set.")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise SharePointConfigError(
            f"GATEWAY_SHAREPOINT_ROOT_URL '{url}' is not a full URL (expected https://<tenant>.sharepoint.com/...)."
        )

    folder_id = parse_qs(parsed.query).get("id")
    server_path = unquote(folder_id[0]) if folder_id else unquote(parsed.path)
    segments = [s for s in server_path.split("/") if s]

    if segments and segments[0].lower() in ("sites", "teams"):
        if len(segments) < 2:
            raise SharePointConfigError(f"GATEWAY_SHAREPOINT_ROOT_URL '{url}' has no site name after '/{segments[0]}'.")
        site_path = "/" + "/".join(segments[:2])
        rest = segments[2:]
    else:
        site_path = ""
        rest = segments

    # A library root opened in the browser ends in /Forms/AllItems.aspx.
    if len(rest) >= 2 and rest[1].lower() == "forms":
        rest = rest[:1]

    return RootLocation(
        hostname=parsed.hostname or parsed.netloc,
        site_path=site_path,
        library=rest[0] if rest else None,
        folder=rest[1:],
    )


class _TokenProvider:
    """Fetches and caches an app-only Graph access token."""

    def __init__(
        self,
        *,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        timeout: float,
        force_ipv4: bool,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = TOKEN_URL.format(tenant=tenant_id)
        self._client_id = client_id
        self._client_secret = client_secret
        if transport is None and force_ipv4:
            transport = httpx.AsyncHTTPTransport(local_address="0.0.0.0")
        self._http = httpx.AsyncClient(timeout=timeout, transport=transport)
        self._lock = asyncio.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    async def get(self) -> str:
        async with self._lock:
            # Refresh a minute early so a token never expires mid-request.
            if self._token and time.monotonic() < self._expires_at - 60:
                return self._token
            try:
                resp = await self._http.post(
                    self._url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "scope": "https://graph.microsoft.com/.default",
                    },
                )
            except httpx.HTTPError as exc:
                raise UpstreamError(f"Could not reach Microsoft sign-in: {exc}") from exc
            if resp.status_code != 200:
                raise UpstreamError(
                    f"Microsoft sign-in rejected the SharePoint app credentials "
                    f"({resp.status_code}): {_graph_error_message(resp)}"
                )
            body = resp.json()
            self._token = body["access_token"]
            self._expires_at = time.monotonic() + float(body.get("expires_in", 3600))
            return self._token

    async def aclose(self) -> None:
        await self._http.aclose()


def _graph_error_message(resp: httpx.Response) -> str:
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    error = body.get("error")
    if isinstance(error, dict):
        return f"{error.get('code', 'error')}: {error.get('message', '')}".strip()
    if isinstance(error, str):
        return f"{error}: {body.get('error_description', '')}".strip()
    return resp.text[:200]


def _is_folder(item: dict[str, Any]) -> bool:
    return "folder" in item


class SharePointClient:
    """Module-level singleton (see `get_sharepoint_client`)."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        token_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        settings = settings or get_settings()
        required = {
            "GATEWAY_SHAREPOINT_ROOT_URL": settings.sharepoint_root_url,
            "GATEWAY_SHAREPOINT_TENANT_ID": settings.sharepoint_tenant_id,
            "GATEWAY_SHAREPOINT_CLIENT_ID": settings.sharepoint_client_id,
            "GATEWAY_SHAREPOINT_CLIENT_SECRET": settings.sharepoint_client_secret,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise SharePointConfigError(f"SharePoint is not configured: set {', '.join(missing)}.")

        self._root_location = parse_root_url(settings.sharepoint_root_url)
        self._max_file_bytes = settings.sharepoint_max_file_bytes
        self._token = _TokenProvider(
            tenant_id=settings.sharepoint_tenant_id,
            client_id=settings.sharepoint_client_id,
            client_secret=settings.sharepoint_client_secret,
            timeout=settings.sharepoint_timeout_seconds,
            force_ipv4=settings.sharepoint_token_force_ipv4,
            transport=token_transport,
        )
        self._http = httpx.AsyncClient(
            base_url=GRAPH_BASE, timeout=settings.sharepoint_timeout_seconds, transport=transport
        )
        self._resolve_lock = asyncio.Lock()
        self._drive_id: str | None = None
        self._root: list[str] = []

    # ------------------------------------------------------------------ HTTP

    async def _send(self, method: str, url: str, *, authorized: bool = True, **kwargs: Any) -> httpx.Response:
        """One Graph call, retried when Graph says it is throttling or busy.

        `authorized=False` is for the pre-authenticated URLs Graph hands back for
        downloads and upload sessions, which must not be sent our bearer token.
        """
        base_headers = dict(kwargs.pop("headers", None) or {})
        for attempt in range(MAX_RETRIES + 1):
            headers = dict(base_headers)
            if authorized:
                headers["Authorization"] = f"Bearer {await self._token.get()}"
            try:
                resp = await self._http.request(method, url, headers=headers, **kwargs)
            except httpx.HTTPError as exc:
                raise UpstreamError(f"Could not reach Microsoft Graph: {exc}") from exc
            if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                try:
                    wait = float(resp.headers.get("Retry-After", 2**attempt))
                except ValueError:
                    wait = float(2**attempt)
                await asyncio.sleep(min(wait, MAX_RETRY_WAIT_SECONDS))
                continue
            return resp
        raise UpstreamError("Microsoft Graph kept answering 'try again later'.")  # pragma: no cover

    @staticmethod
    def _raise_for_status(resp: httpx.Response, what: str) -> None:
        if resp.is_success:
            return
        status = resp.status_code
        detail = _graph_error_message(resp)
        if status == 404:
            raise NotFoundError(f"{what} was not found.")
        if status == 409:
            raise ConflictError(f"{what}: {detail}")
        if status == 423:
            raise ConflictError(f"{what} is locked (checked out, or being saved by someone). Try again shortly.")
        if status == 412:
            raise ConflictError(f"{what} was changed by someone else while this request ran. Try again.")
        if status == 413:
            raise TooLargeError(f"{what}: SharePoint refused the size of this file ({detail}).")
        if status == 400:
            raise BadRequestError(f"{what}: {detail}")
        if status in (401, 403):
            raise UpstreamError(
                f"Microsoft Graph denied access to {what} ({detail}). The SharePoint app registration needs a "
                "grant on this site: Sites.Selected with the write role (or read for a read-only deployment), "
                "or Sites.ReadWrite.All."
            )
        raise UpstreamError(f"Microsoft Graph returned {status} for {what}: {detail}")

    async def _get_json(self, url: str, what: str) -> dict[str, Any]:
        resp = await self._send("GET", url)
        self._raise_for_status(resp, what)
        return resp.json()

    # ------------------------------------------------------- root resolution

    async def _resolve(self) -> tuple[str, list[str]]:
        """Finds the document library and root folder once, then remembers them."""
        if self._drive_id is not None:
            return self._drive_id, self._root
        async with self._resolve_lock:
            if self._drive_id is not None:
                return self._drive_id, self._root

            loc = self._root_location
            try:
                site = await self._get_json(
                    f"/sites/{loc.hostname}:{quote(loc.site_path, safe='/')}" if loc.site_path else f"/sites/{loc.hostname}",
                    f"SharePoint site '{loc.site_path or loc.hostname}'",
                )
                if loc.library is None:
                    drive = await self._get_json(f"/sites/{site['id']}/drive", "the site's default document library")
                    drive_id = drive["id"]
                else:
                    drive_id = await self._find_library(site["id"], loc)

                root = list(loc.folder)
                if root:
                    root_item = await self._get_json(
                        f"/drives/{drive_id}/root:/{encode_path(root)}", "the configured root folder"
                    )
                    if not _is_folder(root_item):
                        raise SharePointConfigError("GATEWAY_SHAREPOINT_ROOT_URL points at a file; it must point at a folder.")
            except NotFoundError as exc:
                raise SharePointConfigError(
                    f"{exc} Check GATEWAY_SHAREPOINT_ROOT_URL and that the app registration has access to the site."
                ) from exc

            self._drive_id, self._root = drive_id, root
            return drive_id, root

    async def _find_library(self, site_id: str, loc: RootLocation) -> str:
        wanted = f"{loc.site_path}/{loc.library}".lower()
        url: str | None = f"/sites/{site_id}/drives?$select=id,name,webUrl"
        while url:
            page = await self._get_json(url, "the site's document libraries")
            for drive in page.get("value", []):
                web_path = unquote(urlparse(drive.get("webUrl", "")).path).rstrip("/").lower()
                if web_path == wanted:
                    return drive["id"]
            url = page.get("@odata.nextLink")
        raise SharePointConfigError(
            f"Document library '{loc.library}' was not found on {loc.site_path or loc.hostname}. "
            "Check GATEWAY_SHAREPOINT_ROOT_URL."
        )

    # ----------------------------------------------------------- item access

    @staticmethod
    def _item_url(drive_id: str, segments: list[str]) -> str:
        if not segments:
            return f"/drives/{drive_id}/root"
        return f"/drives/{drive_id}/root:/{encode_path(segments)}"

    @staticmethod
    def _sub_url(drive_id: str, segments: list[str], suffix: str) -> str:
        """A sub-resource such as /children. Path-addressed items need a closing colon first."""
        if not segments:
            return f"/drives/{drive_id}/root{suffix}"
        return f"/drives/{drive_id}/root:/{encode_path(segments)}:{suffix}"

    async def _try_get_item(self, rel: list[str]) -> dict[str, Any] | None:
        drive_id, root = await self._resolve()
        resp = await self._send("GET", self._item_url(drive_id, root + rel))
        if resp.status_code == 404:
            return None
        self._raise_for_status(resp, f"'{'/'.join(rel)}'")
        return resp.json()

    async def _get_item(self, rel: list[str]) -> dict[str, Any]:
        item = await self._try_get_item(rel)
        if item is None:
            raise NotFoundError(f"'{'/'.join(rel)}' was not found.")
        return item

    async def _require_directory(self, rel: list[str]) -> None:
        if not rel:
            return
        item = await self._try_get_item(rel)
        name = "/".join(rel)
        if item is None:
            raise NotFoundError(f"Directory '{name}' does not exist. Create it first with create_directory.")
        if not _is_folder(item):
            raise ConflictError(f"'{name}' is a file, not a directory.")

    @staticmethod
    def _entry(item: dict[str, Any], rel: list[str]) -> Entry:
        return Entry(
            name=item["name"],
            path="/".join(rel),
            type="directory" if _is_folder(item) else "file",
            size=item.get("size"),
            last_modified=item.get("lastModifiedDateTime"),
            web_url=item.get("webUrl"),
            etag=item.get("eTag"),
        )

    # ------------------------------------------------------------ operations

    async def list_entries(self, rel: list[str], recursive: bool = False) -> tuple[list[Entry], bool]:
        """Lists a directory. Returns (entries, truncated)."""
        drive_id, root = await self._resolve()
        if rel:
            folder = await self._get_item(rel)
            if not _is_folder(folder):
                raise BadRequestError(f"'{'/'.join(rel)}' is a file, not a directory.")

        entries: list[Entry] = []
        queue = [rel]
        while queue:
            current = queue.pop(0)
            url: str | None = self._sub_url(drive_id, root + current, "/children") + f"?$top={LIST_PAGE_SIZE}"
            while url:
                page = await self._get_json(url, f"'{'/'.join(current)}'")
                for child in page.get("value", []):
                    child_rel = current + [child["name"]]
                    entries.append(self._entry(child, child_rel))
                    if len(entries) >= LIST_MAX_ITEMS:
                        return entries, True
                    if recursive and _is_folder(child):
                        queue.append(child_rel)
                url = page.get("@odata.nextLink")
        return entries, False

    async def read_file(self, rel: list[str]) -> tuple[bytes, str | None, Entry]:
        if not rel:
            raise InvalidPathError("A file path is required.")
        item = await self._get_item(rel)
        if _is_folder(item):
            raise BadRequestError(f"'{'/'.join(rel)}' is a directory. Use list_files.")
        data = await self._download(item, rel)
        mime = (item.get("file") or {}).get("mimeType")
        return data, mime, self._entry(item, rel)

    async def _download(self, item: dict[str, Any], rel: list[str]) -> bytes:
        name = "/".join(rel)
        if (item.get("size") or 0) > self._max_file_bytes:
            raise TooLargeError(f"'{name}' is larger than the {self._max_file_bytes} byte limit.")
        drive_id, _ = await self._resolve()
        resp = await self._send("GET", f"/drives/{drive_id}/items/{item['id']}/content")
        if resp.status_code in (301, 302, 303, 307, 308):
            # Graph redirects to a pre-authenticated download URL on another host.
            resp = await self._send("GET", resp.headers["Location"], authorized=False)
        self._raise_for_status(resp, f"'{name}'")
        if len(resp.content) > self._max_file_bytes:
            raise TooLargeError(f"'{name}' is larger than the {self._max_file_bytes} byte limit.")
        return resp.content

    async def write_file(self, rel: list[str], data: bytes, mode: str) -> Entry:
        """mode "w": create or replace. "a": append (creates if missing). "x": create, fail if it exists."""
        if not rel:
            raise InvalidPathError("A file path is required.")
        if len(data) > self._max_file_bytes:
            raise TooLargeError(f"Content is larger than the {self._max_file_bytes} byte limit.")
        await self._require_directory(rel[:-1])

        if mode == "x":
            item = await self._upload(rel, data, conflict="fail")
        elif mode == "w":
            item = await self._upload(rel, data, conflict="replace")
        elif mode == "a":
            existing = await self._try_get_item(rel)
            if existing is None:
                item = await self._upload(rel, data, conflict="fail")
            else:
                if _is_folder(existing):
                    raise BadRequestError(f"'{'/'.join(rel)}' is a directory.")
                combined = await self._download(existing, rel) + data
                if len(combined) > self._max_file_bytes:
                    raise TooLargeError(f"The appended file would exceed the {self._max_file_bytes} byte limit.")
                # Graph has no append, so this is read-modify-write. The ETag makes
                # a concurrent change fail with a 409 instead of being overwritten.
                item = await self._upload(rel, combined, conflict="replace", if_match=existing.get("eTag"))
        else:
            raise BadRequestError(f"Unknown mode '{mode}'. Use 'w', 'a' or 'x'.")
        return self._entry(item, rel)

    async def _upload(self, rel: list[str], data: bytes, *, conflict: str, if_match: str | None = None) -> dict[str, Any]:
        drive_id, root = await self._resolve()
        item_url = self._item_url(drive_id, root + rel)
        what = f"'{'/'.join(rel)}'"
        headers = {"If-Match": if_match} if if_match else {}

        if len(data) <= SIMPLE_UPLOAD_LIMIT:
            resp = await self._send(
                "PUT",
                f"{item_url}:/content?@microsoft.graph.conflictBehavior={conflict}",
                content=data,
                headers={**headers, "Content-Type": "application/octet-stream"},
            )
            self._raise_for_status(resp, what)
            return resp.json()

        session = await self._send(
            "POST",
            f"{item_url}:/createUploadSession",
            json={"item": {"@microsoft.graph.conflictBehavior": conflict}},
            headers=headers,
        )
        self._raise_for_status(session, what)
        upload_url = session.json()["uploadUrl"]
        total = len(data)
        offset = 0
        try:
            while offset < total:
                chunk = data[offset : offset + UPLOAD_CHUNK_BYTES]
                resp = await self._send(
                    "PUT",
                    upload_url,
                    authorized=False,
                    content=chunk,
                    headers={"Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{total}"},
                )
                if resp.status_code in (200, 201):
                    return resp.json()
                if resp.status_code != 202:
                    self._raise_for_status(resp, what)
                    raise UpstreamError(f"Unexpected answer {resp.status_code} while uploading {what}.")
                offset += len(chunk)
            raise UpstreamError(f"Upload of {what} ended without SharePoint confirming the file.")
        except SharePointError:
            # Don't leave a half-finished session behind.
            try:
                await self._send("DELETE", upload_url, authorized=False)
            except SharePointError:
                pass
            raise

    async def delete_file(self, rel: list[str]) -> None:
        if not rel:
            raise InvalidPathError("A file path is required.")
        item = await self._get_item(rel)
        if _is_folder(item):
            raise BadRequestError(f"'{'/'.join(rel)}' is a directory. Use delete_directory.")
        await self._delete(rel)

    async def delete_directory(self, rel: list[str]) -> None:
        """Deletes a folder with everything under it. The root folder itself can never be deleted."""
        if not rel:
            raise InvalidPathError("A directory path is required; the root folder itself cannot be deleted.")
        item = await self._get_item(rel)
        if not _is_folder(item):
            raise BadRequestError(f"'{'/'.join(rel)}' is a file. Use delete_file.")
        await self._delete(rel)

    async def _delete(self, rel: list[str]) -> None:
        drive_id, root = await self._resolve()
        resp = await self._send("DELETE", self._item_url(drive_id, root + rel))
        self._raise_for_status(resp, f"'{'/'.join(rel)}'")

    async def create_directory(self, rel: list[str]) -> bool:
        """Creates the directory and any missing parents. Returns False if it already existed."""
        if not rel:
            raise InvalidPathError("A directory path is required.")
        drive_id, root = await self._resolve()
        created = False
        for depth in range(1, len(rel) + 1):
            prefix = rel[:depth]
            existing = await self._try_get_item(prefix)
            if existing is not None:
                if not _is_folder(existing):
                    raise ConflictError(f"'{'/'.join(prefix)}' already exists and is a file.")
                created = False
                continue
            parent_url = self._sub_url(drive_id, root + prefix[:-1], "/children")
            resp = await self._send(
                "POST",
                parent_url,
                json={"name": prefix[-1], "folder": {}, "@microsoft.graph.conflictBehavior": "fail"},
            )
            if resp.status_code == 409:
                # Someone created it between our check and our create; fine if it is a folder.
                raced = await self._try_get_item(prefix)
                if raced is None or not _is_folder(raced):
                    self._raise_for_status(resp, f"'{'/'.join(prefix)}'")
                created = False
                continue
            self._raise_for_status(resp, f"'{'/'.join(prefix)}'")
            created = True
        return created

    # ------------------------------------------------------------ Excel rows

    async def _require_workbook(self, rel: list[str]) -> None:
        """Raises NotExcelError unless the file is a real .xlsx, not just named like one."""
        name = "/".join(rel)
        if not rel or not rel[-1].lower().endswith(".xlsx"):
            raise NotExcelError(f"'{name}' is not an Excel workbook. Only .xlsx files are supported.")
        item = await self._get_item(rel)
        if _is_folder(item):
            raise BadRequestError(f"'{name}' is a directory, not a workbook.")
        drive_id, _ = await self._resolve()
        # An .xlsx is a zip archive, so it starts with the zip signature. This catches
        # a CSV, an old .xls or an empty file that was only renamed to .xlsx.
        probe = {"Range": "bytes=0-3"}
        resp = await self._send("GET", f"/drives/{drive_id}/items/{item['id']}/content", headers=probe)
        if resp.status_code in (301, 302, 303, 307, 308):
            resp = await self._send("GET", resp.headers["Location"], authorized=False, headers=probe)
        self._raise_for_status(resp, f"'{name}'")
        if resp.content[:4] != b"PK\x03\x04":
            raise NotExcelError(f"'{name}' has an .xlsx name but is not a valid Excel workbook.")

    async def append_excel_row(
        self,
        rel: list[str],
        *,
        values: list[Any] | None,
        row: dict[str, Any] | None,
        table: str | None,
        sheet: str | None,
        allow_formulas: bool,
    ) -> dict[str, Any]:
        """Adds one row to the end of an Excel table, or below the last used row of a sheet.

        Uses Graph's Excel API, so the workbook is edited in place and anything else in
        it (charts, pivots, formatting) is left alone, and someone with the file open
        sees the row appear. A table is the safe target: Excel adds rows to it one at a
        time, so simultaneous callers never overwrite each other. Without a table the
        row goes below the last used row, which two callers at the same moment could
        both pick.
        """
        if (values is None) == (row is None):
            raise BadRequestError("Send exactly one of 'values' (cells in order) or 'row' (cells by column name).")
        if row is not None and not table:
            raise BadRequestError("'row' (cells by column name) needs a 'table'. Without a table, send 'values'.")
        if table and sheet:
            raise BadRequestError("Send 'table' or 'sheet', not both: a table already belongs to one sheet.")

        await self._require_workbook(rel)
        drive_id, root = await self._resolve()
        name = "/".join(rel)

        def workbook_url(suffix: str) -> str:
            return self._sub_url(drive_id, root + rel, f"/workbook{suffix}")

        if table:
            return await self._append_table_row(name, workbook_url, table, values, row, allow_formulas)
        return await self._append_sheet_row(name, workbook_url, sheet, values or [], allow_formulas)

    async def _append_table_row(
        self,
        name: str,
        workbook_url: Callable[[str], str],
        table: str,
        values: list[Any] | None,
        row: dict[str, Any] | None,
        allow_formulas: bool,
    ) -> dict[str, Any]:
        table_url = f"/tables/{quote(table, safe='')}"
        resp = await self._send("GET", workbook_url(f"{table_url}/columns?$select=name"))
        if resp.status_code == 404:
            raise NotFoundError(f"Table '{table}' was not found in '{name}'.")
        self._raise_for_status(resp, f"table '{table}' in '{name}'")
        columns = [c["name"] for c in resp.json().get("value", [])]

        if row is not None:
            unknown = [key for key in row if key not in columns]
            if unknown:
                raise BadRequestError(f"Table '{table}' has no column {unknown}. Its columns are {columns}.")
            ordered = [row.get(column) for column in columns]
        else:
            if len(values or []) != len(columns):
                raise BadRequestError(
                    f"Table '{table}' has {len(columns)} columns {columns} but {len(values or [])} values were sent."
                )
            ordered = list(values or [])

        cells = prepare_cells(ordered, allow_formulas=allow_formulas)
        resp = await self._send("POST", workbook_url(f"{table_url}/rows/add"), json={"values": [cells]})
        self._raise_for_status(resp, f"table '{table}' in '{name}'")
        index = resp.json().get("index")
        return {
            "mode": "table",
            "table": table,
            "sheet": None,
            "row_number": index + 1 if isinstance(index, int) else None,
            "address": None,
        }

    async def _append_sheet_row(
        self,
        name: str,
        workbook_url: Callable[[str], str],
        sheet: str | None,
        values: list[Any],
        allow_formulas: bool,
    ) -> dict[str, Any]:
        cells = prepare_cells(values, allow_formulas=allow_formulas)
        if not cells:
            raise BadRequestError("'values' is empty.")

        if sheet is None:
            resp = await self._send("GET", workbook_url("/worksheets?$select=name,position"))
            self._raise_for_status(resp, f"'{name}'")
            sheets = sorted(resp.json().get("value", []), key=lambda s: s.get("position", 0))
            if not sheets:
                raise NotFoundError(f"'{name}' has no worksheets.")
            sheet = sheets[0]["name"]

        sheet_url = f"/worksheets/{quote(sheet, safe='')}"
        used_url = workbook_url(f"{sheet_url}/usedRange(valuesOnly=true)")
        resp = await self._send("GET", f"{used_url}?$select=rowIndex,rowCount,columnIndex,columnCount")
        if resp.status_code == 404:
            raise NotFoundError(f"Worksheet '{sheet}' was not found in '{name}'.")
        self._raise_for_status(resp, f"worksheet '{sheet}' in '{name}'")
        used = resp.json()

        # A sheet with nothing in it still reports a 1x1 used range (cell A1).
        empty = False
        if used["rowCount"] == 1 and used["columnCount"] == 1:
            probe = await self._send("GET", f"{used_url}?$select=values")
            self._raise_for_status(probe, f"worksheet '{sheet}' in '{name}'")
            empty = probe.json().get("values") in ([[""]], [[None]])

        if empty:
            row_number, start_col = 1, 0
        else:
            row_number, start_col = used["rowIndex"] + used["rowCount"] + 1, used["columnIndex"]
        address = f"{column_letter(start_col)}{row_number}:{column_letter(start_col + len(cells) - 1)}{row_number}"

        resp = await self._send("PATCH", workbook_url(f"{sheet_url}/range(address='{address}')"), json={"values": [cells]})
        self._raise_for_status(resp, f"worksheet '{sheet}' in '{name}'")
        return {"mode": "sheet", "table": None, "sheet": sheet, "row_number": row_number, "address": address}

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._token.aclose()


_client: SharePointClient | None = None


def get_sharepoint_client() -> SharePointClient:
    global _client
    if _client is None:
        _client = SharePointClient()
    return _client


async def close_sharepoint_client() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
