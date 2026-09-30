"""Knowledge sources beyond local files: S3 prefixes, Google Drive folders and web pages.

Each source lists entries (a uri, a title and a version that changes when the content does)
and reads one entry as text. The knowledge base re-reads only entries whose version it has
not indexed yet, and removes documents whose entries disappeared (``on_delete:
propagate``), exactly as for files.

- ``{"type": "s3", "bucket", "prefix", "region", "credentials"}`` (or ``"s3://bucket/prefix"``):
  the same client as file triggers; credentials as JSON keys, or the instance's IAM role.
- ``{"type": "gdrive", "folder_id", "auth"}`` (or ``"gdrive://<folder id>"``): a service
  account with read access to the folder (shared with its email); subfolders included.
  Google Docs and Slides are exported as text, Sheets as CSV; other files are downloaded and
  read like local files (Markdown, text, HTML, CSV, PDF, DOCX, XLSX).
- ``{"type": "url", "url"}`` (or an ``https://`` string): one public web page, fetched with
  the same guard as ``http.get`` (no private addresses).

A source's ``auth``/``credentials`` may also be given once for the corpus (``auth``).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx2

from ..documents.extract import DocumentError, extract, format_of
from ..triggers.files import FileSource, build_source
from .chunk import TEXT_SUFFIXES, html_to_markdown

MAX_BYTES = 25 * 1024 * 1024
DRIVE = "https://www.googleapis.com/drive/v3"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
EXPORTS = {
    "application/vnd.google-apps.document": ("text/plain", "text"),
    "application/vnd.google-apps.presentation": ("text/plain", "text"),
    "application/vnd.google-apps.spreadsheet": ("text/csv", "text"),
}
FOLDER = "application/vnd.google-apps.folder"


class SourceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Entry:
    uri: str
    title: str
    version: str
    ref: Any = None  # what the source needs to read it (a key, a file id)


@dataclass
class Text:
    text: str
    fmt: str  # markdown, text, html


class RemoteSource(Protocol):
    label: str

    async def entries(self) -> list[Entry]: ...

    async def read(self, entry: Entry) -> Text | None: ...


def as_text(data: bytes, name: str) -> Text | None:
    """The indexable text of a file by its name; None for images, scans and unknown formats."""
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if suffix in TEXT_SUFFIXES:
        fmt = TEXT_SUFFIXES[suffix]
        try:
            return Text(data.decode("utf-8-sig"), fmt)
        except UnicodeDecodeError:
            return Text(data.decode("latin-1"), fmt)
    if format_of(name) in ("pdf", "docx", "xlsx"):
        try:
            found = extract(data, name)
        except DocumentError:
            return None
        return None if found.needs_ocr or not found.text.strip() else Text(found.text, "text")
    return None


@dataclass
class S3Knowledge:
    files: FileSource
    label: str

    async def entries(self) -> list[Entry]:
        refs = await self.files.list()
        return [Entry(self.files.uri(r.key), r.key.rsplit("/", 1)[-1], r.version, r.key)
                for r in refs if r.size <= MAX_BYTES]  # fmt: skip

    async def read(self, entry: Entry) -> Text | None:
        return as_text(await self.files.read(str(entry.ref)), str(entry.ref))


@dataclass
class DriveKnowledge:
    folder_id: str
    token: Any  # async () -> access token
    http: httpx2.AsyncClient
    label: str = ""
    _seen: set[str] = field(default_factory=set)

    async def _get(self, url: str, **params: Any) -> httpx2.Response:
        headers = {"Authorization": f"Bearer {await self.token()}"}
        response = await self.http.get(url, params=params, headers=headers)
        if response.status_code >= 400:
            raise SourceError(f"Google Drive: HTTP {response.status_code} for {url}")
        return response

    async def entries(self) -> list[Entry]:
        out: list[Entry] = []
        folders, seen = [self.folder_id], {self.folder_id}
        while folders:
            folder, token = folders.pop(), None
            while True:
                params = {
                    "q": f"'{folder}' in parents and trashed = false",
                    "fields": "nextPageToken, files(id, name, mimeType, md5Checksum,"
                    " modifiedTime, size)",
                    "pageSize": 200, "supportsAllDrives": "true",
                    "includeItemsFromAllDrives": "true",
                }  # fmt: skip
                if token:
                    params["pageToken"] = token
                page = (await self._get(f"{DRIVE}/files", **params)).json()
                for f in page.get("files") or []:
                    if f.get("mimeType") == FOLDER:
                        if f["id"] not in seen:  # a folder shared twice is walked once
                            seen.add(f["id"])
                            folders.append(f["id"])
                        continue
                    if int(f.get("size") or 0) > MAX_BYTES:
                        continue
                    version = f.get("md5Checksum") or f.get("modifiedTime") or ""
                    out.append(Entry(f"gdrive://{f['id']}", str(f.get("name") or f["id"]),
                                     str(version), f))  # fmt: skip
                token = page.get("nextPageToken")
                if not token:
                    break
        return out

    async def read(self, entry: Entry) -> Text | None:
        f = entry.ref
        export = EXPORTS.get(str(f.get("mimeType")))
        if export is not None:
            response = await self._get(f"{DRIVE}/files/{f['id']}/export", mimeType=export[0])
            return Text(response.text, export[1])
        if str(f.get("mimeType", "")).startswith("application/vnd.google-apps."):
            return None  # forms, drawings...: nothing to index
        response = await self._get(f"{DRIVE}/files/{f['id']}", alt="media")
        return as_text(response.content, str(f.get("name") or ""))


@dataclass
class UrlKnowledge:
    url: str
    http: httpx2.AsyncClient
    label: str = ""
    _cache: dict[str, bytes] = field(default_factory=dict)

    async def entries(self) -> list[Entry]:
        response = await self.http.get(self.url, headers={"User-Agent": "dif-general-harness"})
        if response.status_code >= 400:
            raise SourceError(f"{self.url}: HTTP {response.status_code}")
        body = response.content[:MAX_BYTES]
        self._cache[self.url] = body
        title = self.url.rstrip("/").rsplit("/", 1)[-1] or self.url
        return [Entry(self.url, title, hashlib.sha256(body).hexdigest(), response.headers)]

    async def read(self, entry: Entry) -> Text | None:
        body = self._cache.pop(entry.uri, b"")
        kind = str(entry.ref.get("content-type", "")).split(";")[0].strip()
        text = body.decode("utf-8", errors="replace")
        if kind in ("text/html", "application/xhtml+xml"):
            return Text(html_to_markdown(text), "markdown")
        if kind.startswith("text/"):
            return Text(text, "markdown" if kind == "text/markdown" else "text")
        return as_text(body, entry.uri.split("?")[0])


def normalize(src: Any) -> dict[str, Any] | None:
    """A source as a dict; string shorthands (``s3://``, ``gdrive://``, ``https://``, paths)."""
    if isinstance(src, dict):
        return src
    if not isinstance(src, str):
        return None
    if src.startswith("s3://"):
        bucket, _, prefix = src[5:].partition("/")
        return {"type": "s3", "bucket": bucket, "prefix": prefix}
    if src.startswith("gdrive://"):
        return {"type": "gdrive", "folder_id": src[9:]}
    if src.startswith(("https://", "http://")):
        return {"type": "url", "url": src}
    path = src.removeprefix("file://")
    return {"type": "file", "path": path} if path.startswith("/") else None


def build(
    src: dict[str, Any], secret: Any, http: httpx2.AsyncClient, *, s3_client: Any = None,
    guarded_http: httpx2.AsyncClient | None = None,
) -> RemoteSource:  # fmt: skip
    """A remote source (credentials already resolved)."""
    kind = src.get("type")
    if kind == "s3":
        files = build_source({**src, "type": "s3"}, secret, s3_client=s3_client)
        return S3Knowledge(files, f"s3://{src.get('bucket')}/{src.get('prefix') or ''}")
    if kind == "gdrive":
        if not src.get("folder_id"):
            raise SourceError("gdrive sources need a folder_id")
        key = json.loads(secret) if isinstance(secret, str) else secret
        if not isinstance(key, dict) or "client_email" not in key:
            raise SourceError("gdrive sources need a service account key (JSON) as auth")
        from ..tools.packs.google_calendar import GoogleAuth

        auth = GoogleAuth(key, http, scope=DRIVE_SCOPE)
        return DriveKnowledge(str(src["folder_id"]), auth.token, http,
                              f"gdrive://{src['folder_id']}")  # fmt: skip
    if kind == "url":
        if not src.get("url"):
            raise SourceError("url sources need a url")
        return UrlKnowledge(str(src["url"]), guarded_http or http, str(src["url"]))
    raise SourceError(f"unknown knowledge source type {kind!r}")
