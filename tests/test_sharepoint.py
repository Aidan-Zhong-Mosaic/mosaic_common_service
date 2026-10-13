"""SharePoint endpoints against an in-memory stand-in for Microsoft Graph.

The fake implements only the Graph calls the client makes, and enforces the same
rules the real service does where they matter here: bearer auth on Graph calls,
no bearer token on pre-authenticated download/upload URLs, conflict behaviour,
ETag preconditions, and paged listings.
"""
import asyncio
import re

import httpx
import pytest
from fastapi.testclient import TestClient

from app import sharepoint_client
from app.config import Settings
from app.main import app
from app.sharepoint_client import (
    column_letter,
    InvalidPathError,
    SharePointClient,
    SharePointConfigError,
    get_sharepoint_client,
    parse_root_url,
    split_path,
)

ROOT_URL = "https://contoso.sharepoint.com/sites/Team/Shared Documents/Reports"


def make_settings(**overrides) -> Settings:
    values = dict(
        sharepoint_root_url=ROOT_URL,
        sharepoint_tenant_id="tenant-1",
        sharepoint_client_id="client-1",
        sharepoint_client_secret="secret-1",
    )
    values.update(overrides)
    return Settings(**values)


def error(status: int, code: str, message: str = "nope", headers: dict | None = None) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": message}}, headers=headers or {})


class FakeGraph:
    def __init__(self) -> None:
        self.items: dict[str, dict] = {}  # path under the drive -> {"folder": True} or {"data": bytes}
        self.requests: list[httpx.Request] = []
        self.token_calls = 0
        self.fail_once: dict[str, httpx.Response] = {}
        self.page_size = 1000
        self.locked = False
        self.drive_web_url = "https://contoso.sharepoint.com/sites/Team/Shared%20Documents"
        self._counter = 0
        self._sessions: dict[str, dict] = {}
        self.items["Reports"] = self._new(folder=True)

    # -- state helpers

    def _new(self, *, folder: bool = False, data: bytes = b"") -> dict:
        self._counter += 1
        return {"id": f"id{self._counter}", "folder": folder, "data": data, "etag": f'"{self._counter}"'}

    def add_file(self, path: str, data: bytes) -> None:
        self.items[path] = self._new(data=data)

    def add_folder(self, path: str) -> None:
        self.items[path] = self._new(folder=True)

    def add_workbook(self, path: str, *, tables: dict[str, list[str]] | None = None, sheets: list[str] | None = None) -> None:
        """An .xlsx: starts with the zip signature, with tables and sheet grids ({(row, col): value}, 0-based)."""
        self.items[path] = self._new(data=b"PK\x03\x04 pretend workbook")
        self.items[path]["tables"] = {name: {"columns": cols, "rows": []} for name, cols in (tables or {}).items()}
        self.items[path]["sheets"] = {name: {} for name in (sheets or ["Sheet1"])}

    def sheet_row(self, path: str, sheet: str, row_number: int) -> list:
        grid = self.items[path]["sheets"][sheet]
        return [grid[k] for k in sorted(grid, key=lambda k: k[1]) if k[0] == row_number - 1]

    def exists(self, path: str) -> bool:
        return path in self.items

    def data(self, path: str) -> bytes:
        return self.items[path]["data"]

    def _json(self, path: str) -> dict:
        item = self.items[path]
        body = {
            "id": item["id"],
            "name": path.rsplit("/", 1)[-1],
            "eTag": item["etag"],
            "size": len(item["data"]),
            "lastModifiedDateTime": "2026-10-13T09:00:00Z",
            "webUrl": f"https://contoso.sharepoint.com/sites/Team/Shared%20Documents/{path}",
        }
        if item["folder"]:
            body["folder"] = {"childCount": 0}
        else:
            body["file"] = {"mimeType": "text/plain"}
        return body

    def _children(self, folder: str) -> list[str]:
        return sorted(p for p in self.items if (p.rsplit("/", 1)[0] if "/" in p else "") == folder)

    def _put_item(self, path: str, data: bytes, conflict: str, if_match: str | None) -> httpx.Response:
        parent = path.rsplit("/", 1)[0] if "/" in path else ""
        if parent and not self.items.get(parent, {}).get("folder"):
            return error(404, "itemNotFound")
        existing = self.items.get(path)
        if existing and conflict == "fail":
            return error(409, "nameAlreadyExists", "The specified item name already exists.")
        if existing and if_match and if_match != existing["etag"]:
            return error(412, "preconditionFailed")
        self._counter += 1
        self.items[path] = {"id": existing["id"] if existing else f"id{self._counter}", "folder": False, "data": data, "etag": f'"{self._counter}"'}
        return httpx.Response(200 if existing else 201, json=self._json(path))

    # -- token endpoint

    def token_handler(self, request: httpx.Request) -> httpx.Response:
        self.token_calls += 1
        form = dict(httpx.QueryParams(request.content.decode()))
        assert form["grant_type"] == "client_credentials"
        assert form["scope"] == "https://graph.microsoft.com/.default"
        if form["client_secret"] != "secret-1":
            return httpx.Response(401, json={"error": "invalid_client", "error_description": "bad secret"})
        return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})

    # -- Graph and the pre-authenticated hosts

    def graph_handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, method = request.url.host, request.method

        if host == "download.example":
            assert "authorization" not in request.headers, "bearer token must not go to the download URL"
            path = next(p for p, i in self.items.items() if i["id"] == request.url.path.lstrip("/"))
            data = self.items[path]["data"]
            if "range" in request.headers:
                assert request.headers["range"] == "bytes=0-3"
                return httpx.Response(206, content=data[:4])
            return httpx.Response(200, content=data)

        if host == "upload.example":
            assert "authorization" not in request.headers, "bearer token must not go to the upload URL"
            return self._upload_session(request)

        assert host == "graph.microsoft.com"
        if request.headers.get("authorization") != "Bearer tok":
            return error(401, "InvalidAuthenticationToken")
        if method in self.fail_once:
            return self.fail_once.pop(method)

        p = request.url.path.removeprefix("/v1.0")
        if p == "/sites/contoso.sharepoint.com:/sites/Team":
            return httpx.Response(200, json={"id": "site1"})
        if p == "/sites/site1/drives":
            return httpx.Response(200, json={"value": [
                {"id": "other", "name": "Other", "webUrl": "https://contoso.sharepoint.com/sites/Team/Other"},
                {"id": "drive1", "name": "Documents", "webUrl": self.drive_web_url},
            ]})
        m = re.fullmatch(r"/drives/drive1/(.*)", p)
        if not m:
            return error(404, "itemNotFound", f"unrouted {p}")
        return self._drive(request, m.group(1))

    def _drive(self, request: httpx.Request, rest: str) -> httpx.Response:
        method = request.method
        if m := re.fullmatch(r"items/(\w+)/content", rest):
            path = next((p for p, i in self.items.items() if i["id"] == m.group(1)), None)
            if path is None:
                return error(404, "itemNotFound")
            return httpx.Response(302, headers={"Location": f"https://download.example/{m.group(1)}"})
        if m := re.fullmatch(r"root:/(.+):/workbook/(.+)", rest):
            return self._workbook(request, m.group(1), m.group(2))
        if rest == "root/children":
            return self._list("", request)
        if m := re.fullmatch(r"root:/(.+):/children", rest):
            path = m.group(1)
            if method == "POST":
                return self._mkdir(path, request)
            if not self.items.get(path, {}).get("folder"):
                return error(404, "itemNotFound")
            return self._list(path, request)
        if m := re.fullmatch(r"root:/(.+):/content", rest):
            conflict = request.url.params.get("@microsoft.graph.conflictBehavior", "replace")
            return self._put_item(m.group(1), request.content, conflict, request.headers.get("if-match"))
        if m := re.fullmatch(r"root:/(.+):/createUploadSession", rest):
            path = m.group(1)
            conflict = json_body(request)["item"]["@microsoft.graph.conflictBehavior"]
            if path in self.items and conflict == "fail":
                return error(409, "nameAlreadyExists")
            token = f"s{len(self._sessions)}"
            self._sessions[token] = {"path": path, "buffer": b"", "conflict": conflict}
            return httpx.Response(200, json={"uploadUrl": f"https://upload.example/{token}"})
        if m := re.fullmatch(r"root:/(.+)", rest):
            path = m.group(1)
            if path not in self.items:
                return error(404, "itemNotFound")
            if method == "DELETE":
                for key in [k for k in self.items if k == path or k.startswith(path + "/")]:
                    del self.items[key]
                return httpx.Response(204)
            return httpx.Response(200, json=self._json(path))
        return error(404, "itemNotFound", f"unrouted drive path {rest}")

    def _workbook(self, request: httpx.Request, path: str, tail: str) -> httpx.Response:
        item = self.items.get(path)
        if item is None:
            return error(404, "itemNotFound")
        if self.locked:
            return error(423, "resourceLocked")
        if "tables" not in item:
            return error(400, "InvalidRequest", "Could not open the workbook.")

        if m := re.fullmatch(r"tables/([^/]+)/columns", tail):
            table = item["tables"].get(m.group(1))
            if table is None:
                return error(404, "ItemNotFound", "The requested resource doesn't exist.")
            return httpx.Response(200, json={"value": [{"name": c} for c in table["columns"]]})

        if m := re.fullmatch(r"tables/([^/]+)/rows/add", tail):
            table = item["tables"].get(m.group(1))
            if table is None:
                return error(404, "ItemNotFound")
            values = json_body(request)["values"]
            assert len(values) == 1 and isinstance(values[0], list), "rows/add takes a list holding one row"
            table["rows"].append(values[0])
            return httpx.Response(201, json={"index": len(table["rows"]) - 1, "values": values})

        if tail == "worksheets":
            return httpx.Response(200, json={"value": [{"name": n, "position": i} for i, n in enumerate(item["sheets"])]})

        if m := re.fullmatch(r"worksheets/([^/]+)/usedRange\(valuesOnly=true\)", tail):
            grid = item["sheets"].get(m.group(1))
            if grid is None:
                return error(404, "ItemNotFound")
            if grid:
                r0, r1 = min(r for r, _ in grid), max(r for r, _ in grid)
                c0, c1 = min(c for _, c in grid), max(c for _, c in grid)
            else:
                r0 = r1 = c0 = c1 = 0  # an empty sheet reports the single cell A1
            if "values" in request.url.params.get("$select", ""):
                matrix = [[grid.get((r, c), "") for c in range(c0, c1 + 1)] for r in range(r0, r1 + 1)]
                return httpx.Response(200, json={"values": matrix})
            return httpx.Response(200, json={"rowIndex": r0, "rowCount": r1 - r0 + 1, "columnIndex": c0, "columnCount": c1 - c0 + 1})

        if request.method == "PATCH" and (m := re.fullmatch(r"worksheets/([^/]+)/range\(address='([A-Z]+)(\d+):([A-Z]+)(\d+)'\)", tail)):
            grid = item["sheets"].get(m.group(1))
            if grid is None:
                return error(404, "ItemNotFound")
            values = json_body(request)["values"]
            assert len(values) == 1 and m.group(3) == m.group(5), "one row only"
            start = column_index(m.group(2))
            assert column_index(m.group(4)) == start + len(values[0]) - 1, "address width must match the values"
            for offset, value in enumerate(values[0]):
                grid[(int(m.group(3)) - 1, start + offset)] = value
            return httpx.Response(200, json={"address": f"{m.group(1)}!{m.group(2)}{m.group(3)}:{m.group(4)}{m.group(5)}"})

        return error(404, "itemNotFound", f"unrouted workbook path {tail}")

    def _list(self, folder: str, request: httpx.Request) -> httpx.Response:
        skip = int(request.url.params.get("skip", 0))
        children = self._children(folder)
        page = children[skip : skip + self.page_size]
        body: dict = {"value": [self._json(p) for p in page]}
        if skip + self.page_size < len(children):
            base = str(request.url).split("?")[0]
            body["@odata.nextLink"] = f"{base}?skip={skip + self.page_size}"
        return httpx.Response(200, json=body)

    def _mkdir(self, parent: str, request: httpx.Request) -> httpx.Response:
        body = json_body(request)
        path = f"{parent}/{body['name']}" if parent else body["name"]
        if path in self.items:
            return error(409, "nameAlreadyExists")
        self.items[path] = self._new(folder=True)
        return httpx.Response(201, json=self._json(path))

    def _upload_session(self, request: httpx.Request) -> httpx.Response:
        token = request.url.path.lstrip("/")
        session = self._sessions[token]
        if request.method == "DELETE":
            self._sessions.pop(token)
            return httpx.Response(204)
        start, end, total = map(int, re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", request.headers["content-range"]).groups())
        assert start == len(session["buffer"]), "chunks must arrive in order"
        session["buffer"] += request.content
        if end + 1 < total:
            return httpx.Response(202, json={"nextExpectedRanges": [f"{end + 1}-"]})
        return self._put_item(session["path"], session["buffer"], session["conflict"], None)


def json_body(request: httpx.Request) -> dict:
    import json

    return json.loads(request.content)


def column_index(letters: str) -> int:
    index = 0
    for ch in letters:
        index = index * 26 + (ord(ch) - 64)
    return index - 1


@pytest.fixture
def graph() -> FakeGraph:
    return FakeGraph()


@pytest.fixture
def sp(graph: FakeGraph) -> SharePointClient:
    return SharePointClient(
        make_settings(),
        transport=httpx.MockTransport(graph.graph_handler),
        token_transport=httpx.MockTransport(graph.token_handler),
    )


@pytest.fixture
def api(sp: SharePointClient):
    app.dependency_overrides[get_sharepoint_client] = lambda: sp
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


# ---------------------------------------------------------------- path rules


def test_split_path_ignores_extra_slashes():
    assert split_path("/a//b/./c/") == ["a", "b", "c"]
    assert split_path("") == []
    assert split_path("/") == []


@pytest.mark.parametrize("bad", ["../secret", "a/../../b", "a/..", "a\\b", "a/b:c", "a/b?c", "a/b*", "a/trailing.", "a/ spaced", "a/\x01"])
def test_split_path_rejects_unsafe_paths(bad):
    with pytest.raises(InvalidPathError):
        split_path(bad)


# ---------------------------------------------------------------- root URL


def test_parse_plain_folder_url():
    loc = parse_root_url(ROOT_URL)
    assert (loc.hostname, loc.site_path, loc.library, loc.folder) == ("contoso.sharepoint.com", "/sites/Team", "Shared Documents", ["Reports"])


def test_parse_encoded_and_nested_url():
    loc = parse_root_url("https://contoso.sharepoint.com/sites/Team/Shared%20Documents/Reports/2026/Q3")
    assert (loc.library, loc.folder) == ("Shared Documents", ["Reports", "2026", "Q3"])


def test_parse_address_bar_url_with_id_parameter():
    url = (
        "https://contoso.sharepoint.com/sites/Team/Shared%20Documents/Forms/AllItems.aspx"
        "?id=%2Fsites%2FTeam%2FShared%20Documents%2FReports&viewid=abc"
    )
    loc = parse_root_url(url)
    assert (loc.site_path, loc.library, loc.folder) == ("/sites/Team", "Shared Documents", ["Reports"])


def test_parse_the_real_data_office_address_bar_url():
    url = (
        "https://mymosaic1609.sharepoint.com/sites/DataOffice/Shared%20Documents/Forms/AllItems.aspx"
        "?id=%2Fsites%2FDataOffice%2FShared%20Documents%2FMosAIc%5Fsharepoint"
        "&viewid=94e8ae35%2D6c73%2D4fb6%2D8b4c%2D7bcd8a0ac457&as=json"
    )
    loc = parse_root_url(url)
    assert (loc.hostname, loc.site_path, loc.library, loc.folder) == (
        "mymosaic1609.sharepoint.com", "/sites/DataOffice", "Shared Documents", ["MosAIc_sharepoint"],
    )


def test_parse_keeps_a_literal_percent_in_a_folder_name():
    # %2541 in the address bar is the folder name "100%41"; decoding twice would turn it into "100A".
    url = "https://contoso.sharepoint.com/sites/T/Shared%20Documents/Forms/AllItems.aspx?id=%2Fsites%2FT%2FShared%20Documents%2F100%2541"
    assert parse_root_url(url).folder == ["100%41"]
    assert parse_root_url("https://contoso.sharepoint.com/sites/T/Shared%20Documents/100%2541").folder == ["100%41"]


def test_parse_library_root_and_site_root():
    loc = parse_root_url("https://contoso.sharepoint.com/sites/Team/Shared%20Documents/Forms/AllItems.aspx")
    assert (loc.library, loc.folder) == ("Shared Documents", [])
    loc = parse_root_url("https://contoso.sharepoint.com/sites/Team")
    assert (loc.library, loc.folder) == (None, [])
    loc = parse_root_url("https://contoso.sharepoint.com/Shared%20Documents/Reports")
    assert (loc.site_path, loc.library, loc.folder) == ("", "Shared Documents", ["Reports"])


@pytest.mark.parametrize("bad", ["", "   ", "contoso.sharepoint.com/sites/Team", "ftp://x/sites/Team", "https://contoso.sharepoint.com/sites"])
def test_parse_rejects_bad_root_url(bad):
    with pytest.raises(SharePointConfigError):
        parse_root_url(bad)


# ---------------------------------------------------------------- configuration


def test_client_lists_every_missing_setting():
    with pytest.raises(SharePointConfigError) as exc:
        SharePointClient(Settings(sharepoint_root_url="", sharepoint_tenant_id="", sharepoint_client_id="", sharepoint_client_secret=""))
    for name in ("ROOT_URL", "TENANT_ID", "CLIENT_ID", "CLIENT_SECRET"):
        assert f"GATEWAY_SHAREPOINT_{name}" in str(exc.value)


def test_unconfigured_service_answers_503_not_500(monkeypatch):
    monkeypatch.setattr(sharepoint_client, "get_settings", lambda: Settings(sharepoint_root_url=""))
    monkeypatch.setattr(sharepoint_client, "_client", None)
    with TestClient(app) as client:
        resp = client.get("/sharepoint/list_files")
    assert resp.status_code == 503
    assert "GATEWAY_SHAREPOINT_ROOT_URL" in resp.json()["detail"]


def test_missing_library_is_a_config_error(graph, api):
    graph.drive_web_url = "https://contoso.sharepoint.com/sites/Team/Something%20Else"
    resp = api.get("/sharepoint/list_files")
    assert resp.status_code == 503
    assert "Shared Documents" in resp.json()["detail"]


def test_missing_root_folder_is_a_config_error(graph, api):
    del graph.items["Reports"]
    resp = api.get("/sharepoint/list_files")
    assert resp.status_code == 503
    assert "GATEWAY_SHAREPOINT_ROOT_URL" in resp.json()["detail"]


# ---------------------------------------------------------------- listing


def test_list_root_and_subdirectory(graph, api):
    graph.add_file("Reports/a.txt", b"A")
    graph.add_folder("Reports/sub")
    graph.add_file("Reports/sub/b.txt", b"BB")
    body = api.get("/sharepoint/list_files").json()
    assert {(i["path"], i["type"]) for i in body["items"]} == {("a.txt", "file"), ("sub", "directory")}
    assert body["count"] == 2 and body["truncated"] is False

    body = api.get("/sharepoint/list_files", params={"path": "sub"}).json()
    assert [(i["name"], i["path"], i["size"]) for i in body["items"]] == [("b.txt", "sub/b.txt", 2)]


def test_list_recursive_and_paging(graph, api):
    graph.page_size = 2
    for n in range(5):
        graph.add_file(f"Reports/f{n}.txt", b"x")
    graph.add_folder("Reports/d")
    graph.add_file("Reports/d/deep.txt", b"x")
    paths = {i["path"] for i in api.get("/sharepoint/list_files", params={"recursive": "true"}).json()["items"]}
    assert paths == {f"f{n}.txt" for n in range(5)} | {"d", "d/deep.txt"}


def test_list_stops_at_item_limit(graph, api, monkeypatch):
    monkeypatch.setattr(sharepoint_client, "LIST_MAX_ITEMS", 3)
    for n in range(6):
        graph.add_file(f"Reports/f{n}.txt", b"x")
    body = api.get("/sharepoint/list_files").json()
    assert body["count"] == 3 and body["truncated"] is True


def test_list_errors(graph, api):
    graph.add_file("Reports/a.txt", b"A")
    assert api.get("/sharepoint/list_files", params={"path": "nope"}).status_code == 404
    assert api.get("/sharepoint/list_files", params={"path": "a.txt"}).status_code == 400


# ---------------------------------------------------------------- reading


def test_read_file_returns_bytes_and_type(graph, api):
    graph.add_file("Reports/hello world.txt", "héllo".encode())
    resp = api.get("/sharepoint/read_file", params={"path": "hello world.txt"})
    assert resp.status_code == 200
    assert resp.content == "héllo".encode()
    assert resp.headers["content-type"].startswith("text/plain")
    assert "hello%20world.txt" in resp.headers["content-disposition"]


def test_read_file_errors(graph, api):
    graph.add_folder("Reports/sub")
    assert api.get("/sharepoint/read_file", params={"path": "missing.txt"}).status_code == 404
    assert api.get("/sharepoint/read_file", params={"path": "sub"}).status_code == 400
    assert api.get("/sharepoint/read_file", params={"path": "../x"}).status_code == 400


def test_read_file_too_large(graph, api, monkeypatch):
    graph.add_file("Reports/big.bin", b"x" * 20)
    monkeypatch.setattr(sp_attr := api.app.dependency_overrides[get_sharepoint_client](), "_max_file_bytes", 10)
    assert api.get("/sharepoint/read_file", params={"path": "big.bin"}).status_code == 413


def test_download_url_never_receives_the_bearer_token(graph, api):
    graph.add_file("Reports/a.txt", b"A")
    assert api.get("/sharepoint/read_file", params={"path": "a.txt"}).status_code == 200  # the fake asserts on the header


# ---------------------------------------------------------------- writing


def test_write_modes(graph, api):
    def write(content, mode, path="out.txt"):
        return api.post("/sharepoint/write_file", json={"path": path, "content": content, "mode": mode})

    assert write("one", "w").status_code == 200
    assert graph.data("Reports/out.txt") == b"one"
    assert write("two", "w").status_code == 200
    assert graph.data("Reports/out.txt") == b"two"
    assert write("+three", "a").status_code == 200
    assert graph.data("Reports/out.txt") == b"two+three"
    assert write("new", "a", "fresh.txt").status_code == 200
    assert graph.data("Reports/fresh.txt") == b"new"
    resp = write("again", "x")
    assert resp.status_code == 409 and graph.data("Reports/out.txt") == b"two+three"
    assert write("brand new", "x", "exclusive.txt").status_code == 200


def test_write_returns_the_item(graph, api):
    body = api.post("/sharepoint/write_file", json={"path": "r.txt", "content": "abc"}).json()
    assert (body["name"], body["path"], body["type"], body["size"]) == ("r.txt", "r.txt", "file", 3)


def test_write_binary_as_base64(graph, api):
    payload = bytes(range(256))
    import base64

    resp = api.post("/sharepoint/write_file", json={"path": "b.bin", "content": base64.b64encode(payload).decode(), "content_encoding": "base64"})
    assert resp.status_code == 200 and graph.data("Reports/b.bin") == payload
    assert api.post("/sharepoint/write_file", json={"path": "c.bin", "content": "not base64!!", "content_encoding": "base64"}).status_code == 400


def test_write_needs_an_existing_parent_directory(graph, api):
    resp = api.post("/sharepoint/write_file", json={"path": "nodir/x.txt", "content": "x"})
    assert resp.status_code == 404 and "create_directory" in resp.json()["detail"]
    graph.add_file("Reports/afile", b"x")
    assert api.post("/sharepoint/write_file", json={"path": "afile/x.txt", "content": "x"}).status_code == 409


def test_write_rejects_path_escape_and_bad_mode(graph, api):
    assert api.post("/sharepoint/write_file", json={"path": "../evil.txt", "content": "x"}).status_code == 400
    assert api.post("/sharepoint/write_file", json={"path": "ok.txt", "content": "x", "mode": "z"}).status_code == 422
    assert not any(p.startswith("..") for p in graph.items)


def test_append_loses_to_a_concurrent_change(graph, api):
    graph.add_file("Reports/log.txt", b"start")
    original_put = graph._put_item

    def sneaky(path, data, conflict, if_match):
        graph.items[path]["etag"] = '"changed-by-someone-else"'  # someone edits between our read and our write
        return original_put(path, data, conflict, if_match)

    graph._put_item = sneaky
    resp = api.post("/sharepoint/write_file", json={"path": "log.txt", "content": "+more", "mode": "a"})
    assert resp.status_code == 409
    assert graph.data("Reports/log.txt") == b"start"


def test_large_write_uses_an_upload_session_in_chunks(graph, api, monkeypatch):
    monkeypatch.setattr(sharepoint_client, "UPLOAD_CHUNK_BYTES", 320 * 1024 * 2)
    big = "x" * (5 * 1024 * 1024)
    resp = api.post("/sharepoint/write_file", json={"path": "big.txt", "content": big})
    assert resp.status_code == 200
    assert graph.data("Reports/big.txt") == big.encode()
    assert any(r.url.host == "upload.example" for r in graph.requests)
    assert sum(1 for r in graph.requests if r.url.host == "upload.example") > 2


def test_large_exclusive_write_conflict_is_clean(graph, api):
    graph.add_file("Reports/big.txt", b"old")
    resp = api.post("/sharepoint/write_file", json={"path": "big.txt", "content": "x" * (5 * 1024 * 1024), "mode": "x"})
    assert resp.status_code == 409 and graph.data("Reports/big.txt") == b"old"


def test_write_over_the_size_limit(graph, api, monkeypatch):
    monkeypatch.setattr(api.app.dependency_overrides[get_sharepoint_client](), "_max_file_bytes", 5)
    assert api.post("/sharepoint/write_file", json={"path": "x.txt", "content": "123456"}).status_code == 413


# ---------------------------------------------------------------- create_file


def test_create_file(graph, api):
    resp = api.post("/sharepoint/create_file", json={"path": "new.txt"})
    assert resp.status_code == 201 and graph.data("Reports/new.txt") == b""
    assert api.post("/sharepoint/create_file", json={"path": "new.txt", "content": "x"}).status_code == 409
    assert api.post("/sharepoint/create_file", json={"path": "seeded.txt", "content": "hi"}).status_code == 201
    assert graph.data("Reports/seeded.txt") == b"hi"


# ---------------------------------------------------------------- deleting


def test_delete_file(graph, api):
    graph.add_file("Reports/a.txt", b"A")
    graph.add_folder("Reports/sub")
    assert api.post("/sharepoint/delete_file", json={"path": "a.txt"}).json() == {"path": "a.txt", "deleted": True}
    assert not graph.exists("Reports/a.txt")
    assert api.post("/sharepoint/delete_file", json={"path": "a.txt"}).status_code == 404
    assert api.post("/sharepoint/delete_file", json={"path": "sub"}).status_code == 400
    assert graph.exists("Reports/sub")


def test_delete_directory_is_recursive(graph, api):
    graph.add_folder("Reports/sub")
    graph.add_folder("Reports/sub/deeper")
    graph.add_file("Reports/sub/a.txt", b"A")
    graph.add_file("Reports/sub/deeper/b.txt", b"B")
    graph.add_file("Reports/keep.txt", b"K")
    assert api.post("/sharepoint/delete_directory", json={"path": "sub"}).status_code == 200
    assert not any(p.startswith("Reports/sub") for p in graph.items)
    assert graph.exists("Reports/keep.txt")


def test_delete_directory_refuses_the_root_and_files(graph, api):
    graph.add_file("Reports/a.txt", b"A")
    assert api.post("/sharepoint/delete_directory", json={"path": "/"}).status_code == 400
    assert api.post("/sharepoint/delete_directory", json={"path": "."}).status_code == 400
    assert api.post("/sharepoint/delete_directory", json={"path": "a.txt"}).status_code == 400
    assert api.post("/sharepoint/delete_directory", json={"path": "nope"}).status_code == 404
    assert graph.exists("Reports") and graph.exists("Reports/a.txt")


# ---------------------------------------------------------------- directories


def test_create_directory_makes_parents_and_is_repeatable(graph, api):
    resp = api.post("/sharepoint/create_directory", json={"path": "2026/q3/drafts"})
    assert resp.status_code == 200 and resp.json() == {"path": "2026/q3/drafts", "created": True}
    assert all(graph.items[p]["folder"] for p in ("Reports/2026", "Reports/2026/q3", "Reports/2026/q3/drafts"))
    assert api.post("/sharepoint/create_directory", json={"path": "2026/q3/drafts"}).json()["created"] is False
    assert api.post("/sharepoint/create_directory", json={"path": "2026/q3/other"}).json()["created"] is True


def test_create_directory_conflicts_with_a_file(graph, api):
    graph.add_file("Reports/taken", b"x")
    assert api.post("/sharepoint/create_directory", json={"path": "taken"}).status_code == 409
    assert api.post("/sharepoint/create_directory", json={"path": "taken/inside"}).status_code == 409
    assert api.post("/sharepoint/create_directory", json={"path": "/"}).status_code == 400


# ---------------------------------------------------------------- read-only mode


def test_read_only_blocks_every_change_but_not_reads(graph, api, monkeypatch):
    graph.add_file("Reports/a.txt", b"A")
    monkeypatch.setattr("app.routes.sharepoint.get_settings", lambda: Settings(sharepoint_read_only=True))
    for route, body in [
        ("write_file", {"path": "a.txt", "content": "x"}),
        ("create_file", {"path": "n.txt"}),
        ("delete_file", {"path": "a.txt"}),
        ("create_directory", {"path": "d"}),
        ("delete_directory", {"path": "d"}),
        ("append_excel_row", {"path": "b.xlsx", "values": [1]}),
    ]:
        resp = api.post(f"/sharepoint/{route}", json=body)
        assert resp.status_code == 403, route
        assert "READ_ONLY" in resp.json()["detail"]
    assert graph.data("Reports/a.txt") == b"A"
    assert api.get("/sharepoint/read_file", params={"path": "a.txt"}).status_code == 200
    assert api.get("/sharepoint/list_files").status_code == 200


# ---------------------------------------------------------------- auth and Graph errors


def test_token_is_fetched_once_and_reused(graph, api):
    graph.add_file("Reports/a.txt", b"A")
    for _ in range(3):
        api.get("/sharepoint/list_files")
    assert graph.token_calls == 1


def test_bad_app_credentials_are_a_502_with_the_reason(graph):
    sp = SharePointClient(
        make_settings(sharepoint_client_secret="wrong"),
        transport=httpx.MockTransport(graph.graph_handler),
        token_transport=httpx.MockTransport(graph.token_handler),
    )
    app.dependency_overrides[get_sharepoint_client] = lambda: sp
    try:
        with TestClient(app) as client:
            resp = client.get("/sharepoint/list_files")
    finally:
        app.dependency_overrides.clear()
    assert resp.status_code == 502 and "rejected" in resp.json()["detail"]


def test_graph_access_denied_explains_the_permission_needed(graph, api):
    graph.fail_once["GET"] = error(403, "accessDenied", "Access denied")
    resp = api.get("/sharepoint/list_files")
    assert resp.status_code == 502
    assert "Sites.Selected" in resp.json()["detail"]


def test_throttling_is_retried(graph, api, monkeypatch):
    async def no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    graph.fail_once["GET"] = error(429, "activityLimitReached", headers={"Retry-After": "1"})
    assert api.get("/sharepoint/list_files").status_code == 200
    assert sum(1 for r in graph.requests if r.url.path.endswith("/drives") or "sites" in r.url.path) >= 2


# ---------------------------------------------------------------- encoding of awkward names


def test_names_with_spaces_hash_and_percent_round_trip(graph, api):
    name = "Q3 report #2 100%.txt"
    assert api.post("/sharepoint/write_file", json={"path": f"notes/{name}", "content": "x"}).status_code == 404  # parent missing
    api.post("/sharepoint/create_directory", json={"path": "notes"})
    assert api.post("/sharepoint/write_file", json={"path": f"notes/{name}", "content": "ok"}).status_code == 200
    assert graph.data(f"Reports/notes/{name}") == b"ok"
    resp = api.get("/sharepoint/read_file", params={"path": f"notes/{name}"})
    assert resp.status_code == 200 and resp.content == b"ok"


# ---------------------------------------------------------------- Excel rows

BOOK = "Reports/orders.xlsx"


def append(api, **body):
    body.setdefault("path", "orders.xlsx")
    return api.post("/sharepoint/append_excel_row", json=body)


def test_column_letters():
    assert [column_letter(i) for i in (0, 1, 25, 26, 27, 51, 52, 701, 702)] == ["A", "B", "Z", "AA", "AB", "AZ", "BA", "ZZ", "AAA"]


def test_table_append_by_values(graph, api):
    graph.add_workbook(BOOK, tables={"Orders": ["Name", "Qty", "Paid"]})
    resp = append(api, table="Orders", values=["Ada", 3, True])
    assert resp.status_code == 201
    assert resp.json() == {"path": "orders.xlsx", "mode": "table", "table": "Orders", "sheet": None, "row_number": 1, "address": None}
    assert append(api, table="Orders", values=["Bob", 4.5, False]).json()["row_number"] == 2
    assert graph.items[BOOK]["tables"]["Orders"]["rows"] == [["Ada", 3, True], ["Bob", 4.5, False]]


def test_table_append_by_column_name_in_any_order(graph, api):
    graph.add_workbook(BOOK, tables={"Orders": ["Name", "Qty", "Paid"]})
    assert append(api, table="Orders", row={"Qty": 7, "Name": "Cy"}).status_code == 201
    assert graph.items[BOOK]["tables"]["Orders"]["rows"] == [["Cy", 7, ""]]


def test_empty_cells_and_awkward_table_names(graph, api):
    graph.add_workbook(BOOK, tables={"Q3 Orders & More": ["A", "B"]})
    assert append(api, table="Q3 Orders & More", values=[None, "x"]).status_code == 201
    assert graph.items[BOOK]["tables"]["Q3 Orders & More"]["rows"] == [["", "x"]]


def test_table_append_errors(graph, api):
    graph.add_workbook(BOOK, tables={"Orders": ["Name", "Qty"]})
    resp = append(api, table="Orders", row={"Nme": "typo"})
    assert resp.status_code == 400 and "Name" in resp.json()["detail"] and "Nme" in resp.json()["detail"]
    resp = append(api, table="Orders", values=["only one"])
    assert resp.status_code == 400 and "2 columns" in resp.json()["detail"]
    resp = append(api, table="Nope", values=["a", 1])
    assert resp.status_code == 404 and "Nope" in resp.json()["detail"]
    assert append(api, path="missing.xlsx", table="Orders", values=["a", 1]).status_code == 404
    assert graph.items[BOOK]["tables"]["Orders"]["rows"] == []


def test_formula_text_is_refused_unless_asked_for(graph, api):
    graph.add_workbook(BOOK, tables={"Orders": ["Name", "Note"]})
    for danger in ["=HYPERLINK(\"http://evil\")", "+1+1", "-cmd", "@SUM(A1)", "\tsneaky"]:
        resp = append(api, table="Orders", values=["x", danger])
        assert resp.status_code == 400, danger
        assert "formula" in resp.json()["detail"]
    assert graph.items[BOOK]["tables"]["Orders"]["rows"] == []
    assert append(api, table="Orders", values=["x", -5]).status_code == 201  # a real number is fine
    assert append(api, table="Orders", values=["x", "=SUM(A1:A2)"], allow_formulas=True).status_code == 201
    assert graph.items[BOOK]["tables"]["Orders"]["rows"] == [["x", -5], ["x", "=SUM(A1:A2)"]]


@pytest.mark.parametrize("name, data", [
    ("data.csv", b"a,b\n1,2\n"),
    ("legacy.xls", b"\xd0\xcf\x11\xe0 old binary excel"),
    ("notes.txt", b"hello"),
    ("report.docx", b"PK\x03\x04 a real zip, wrong kind"),
    ("renamed.xlsx", b"a,b\n1,2\n"),
    ("empty.xlsx", b""),
])
def test_files_that_are_not_excel_get_415(graph, api, name, data):
    graph.add_file(f"Reports/{name}", data)
    resp = append(api, path=name, table="Orders", values=["a", 1])
    assert resp.status_code == 415, name
    assert "Excel" in resp.json()["detail"]
    resp = append(api, path=name, values=["a", 1])
    assert resp.status_code == 415, name


def test_missing_non_excel_name_and_folder_named_like_a_workbook(graph, api):
    assert append(api, path="nothing.csv", values=["a"]).status_code == 415
    graph.add_folder("Reports/tricky.xlsx")
    assert append(api, path="tricky.xlsx", values=["a"]).status_code == 400
    assert append(api, path="orders.XLSX", values=["a"]).status_code == 404  # upper-case extension is accepted, then not found


def test_only_the_first_bytes_are_downloaded_to_check_the_file(graph, api):
    graph.add_workbook(BOOK, tables={"Orders": ["A"]})
    assert append(api, table="Orders", values=[1]).status_code == 201
    downloads = [r for r in graph.requests if r.url.host == "download.example"]
    assert downloads and all(r.headers.get("range") == "bytes=0-3" for r in downloads)


def test_sheet_append_to_empty_sheet_then_below_the_data(graph, api):
    graph.add_workbook(BOOK, sheets=["Data"])
    first = append(api, values=["x", 1, 2])
    assert first.status_code == 201
    assert first.json() == {"path": "orders.xlsx", "mode": "sheet", "table": None, "sheet": "Data", "row_number": 1, "address": "A1:C1"}
    assert append(api, values=["y", 3, 4]).json()["address"] == "A2:C2"
    assert graph.sheet_row(BOOK, "Data", 1) == ["x", 1, 2] and graph.sheet_row(BOOK, "Data", 2) == ["y", 3, 4]


def test_sheet_append_with_a_single_cell_row(graph, api):
    graph.add_workbook(BOOK)
    assert append(api, values=["a"]).json()["address"] == "A1:A1"
    assert append(api, values=["b"]).json()["address"] == "A2:A2"  # a one-cell sheet that has data is not "empty"


def test_sheet_append_follows_the_used_range(graph, api):
    graph.add_workbook(BOOK, sheets=["Other", "Data"])
    grid = graph.items[BOOK]["sheets"]["Data"]
    grid.update({(1, 1): "h1", (1, 2): "h2", (2, 1): "a", (2, 2): "b", (3, 1): "c", (3, 2): "d"})  # B2:C4
    resp = append(api, sheet="Data", values=["e", "f"])
    assert resp.json()["address"] == "B5:C5" and resp.json()["row_number"] == 5
    assert graph.sheet_row(BOOK, "Data", 5) == ["e", "f"]
    assert graph.items[BOOK]["sheets"]["Other"] == {}


def test_sheet_append_defaults_to_the_first_sheet_and_reports_missing_ones(graph, api):
    graph.add_workbook(BOOK, sheets=["First", "Second"])
    assert append(api, values=["x"]).json()["sheet"] == "First"
    resp = append(api, sheet="Third", values=["x"])
    assert resp.status_code == 404 and "Third" in resp.json()["detail"]


@pytest.mark.parametrize("body", [
    {},
    {"values": [1], "row": {"A": 1}, "table": "T"},
    {"row": {"A": 1}},
    {"values": [1], "table": "T", "sheet": "S"},
    {"values": "not a list"},
])
def test_bad_request_shapes_are_rejected_before_any_work(graph, api, body):
    graph.add_workbook(BOOK, tables={"T": ["A"]})
    assert append(api, **body).status_code == 422
    assert not any("workbook" in r.url.path for r in graph.requests)


def test_locked_workbook_is_a_409(graph, api):
    graph.add_workbook(BOOK, tables={"Orders": ["A"]})
    graph.locked = True
    resp = append(api, table="Orders", values=[1])
    assert resp.status_code == 409 and "locked" in resp.json()["detail"]


# ---------------------------------------------------------------- audit


def test_every_call_is_audited(graph, api, monkeypatch):
    events = []
    monkeypatch.setattr("app.routes.sharepoint.log_sharepoint", lambda **kw: events.append(kw))
    api.post("/sharepoint/create_file", json={"path": "a.txt"})
    api.get("/sharepoint/read_file", params={"path": "missing.txt"})
    assert [(e["operation"], e["path"], e["status"]) for e in events] == [
        ("create_file", "a.txt", "ok"),
        ("read_file", "missing.txt", "failed"),
    ]
