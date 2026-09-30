"""File sources for ``file`` triggers: new files in a folder or an S3 bucket start work.

A source lists objects (key, size, version) and reads one by key. The headless runtime polls
each file trigger's source every ``poll`` (default one minute), reads only objects it has not
seen in that version, and fires the trigger once per new ``dedupe_key`` value (for example
the content hash), so a file copied twice or re-uploaded with the same bytes starts nothing.

The event a file trigger fires with is ``{"file": {...}}``:

- ``key`` (path within the source), ``name``, ``size``, ``sha256``, ``uri``
  (``file:///...`` or ``s3://bucket/key``), ``content_type`` (from the extension);
- ``text``: the content itself, for text files (XML, JSON, CSV, Markdown, plain text) up to
  ``inline_bytes`` (default 64 KB), so an agent can read a CFDI XML without another tool.
  Binary files are read through the ``documents`` tools by ``uri``.

Sources:

- ``{"type": "folder", "path": "/data/inbox"}``: a directory, walked recursively (hidden
  files and partial uploads ending in ``.part``/``.tmp`` are skipped);
- ``{"type": "s3", "bucket": "...", "prefix": "inbox/", "region": "...",
  "credentials": {"$secret": "storage"}}``: with no credentials the instance's IAM role is
  used; a credentials secret is JSON with ``access_key_id`` and ``secret_access_key``.
  ``boto3`` is an optional dependency (``.[aws]``); tests inject a client.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import mimetypes
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DEFAULT_POLL_S = 60.0
DEFAULT_MAX_BYTES = 25 * 1024 * 1024
DEFAULT_INLINE_BYTES = 64 * 1024
SCAN_LIMIT = 100  # new files read per scan; the rest wait for the next one
TEXT_TYPES = {".xml", ".json", ".csv", ".txt", ".md", ".tsv", ".yaml", ".yml", ".html"}
PARTIAL = (".part", ".tmp", ".crdownload", "~")


class FileSourceError(ValueError):
    pass


@dataclass(frozen=True)
class FileRef:
    key: str
    size: int
    version: str  # changes when the object changes (mtime, ETag)


class FileSource(Protocol):
    kind: str

    async def list(self) -> list[FileRef]: ...

    async def read(self, key: str) -> bytes: ...

    def uri(self, key: str) -> str: ...

    def key(self, uri: str) -> str | None: ...


class FolderSource:
    kind = "folder"

    def __init__(self, path: str) -> None:
        self.root = Path(path)

    def _list(self) -> list[FileRef]:
        if not self.root.is_dir():
            raise FileSourceError(f"folder {self.root} does not exist")
        refs = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            for name in sorted(filenames):
                if name.startswith(".") or name.endswith(PARTIAL):
                    continue
                path = Path(dirpath) / name
                stat = path.stat()
                key = path.relative_to(self.root).as_posix()
                refs.append(FileRef(key, stat.st_size, f"{stat.st_mtime_ns}:{stat.st_size}"))
        return refs

    async def list(self) -> list[FileRef]:
        return await asyncio.to_thread(self._list)

    def _path(self, key: str) -> Path:
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise FileSourceError(f"{key!r} is outside the folder")
        return path

    async def read(self, key: str) -> bytes:
        return await asyncio.to_thread(self._path(key).read_bytes)

    def uri(self, key: str) -> str:
        return self._path(key).as_uri()

    def key(self, uri: str) -> str | None:
        prefix = self.root.resolve().as_uri() + "/"
        return uri[len(prefix) :] if uri.startswith(prefix) else None


class S3Source:
    kind = "s3"

    def __init__(
        self,
        bucket: str,
        prefix: str = "",
        *,
        region: str | None = None,
        credentials: dict[str, str] | None = None,
        client: Any = None,
    ) -> None:
        if client is None:
            import boto3  # type: ignore[import-not-found]

            keys = {}
            if credentials:
                keys = {
                    "aws_access_key_id": credentials["access_key_id"],
                    "aws_secret_access_key": credentials["secret_access_key"],
                }
            client = boto3.client("s3", region_name=region, **keys)
        self.client = client
        self.bucket = bucket
        self.prefix = prefix

    def _list(self) -> list[FileRef]:
        refs, token = [], None
        while True:
            kw: dict[str, Any] = {"Bucket": self.bucket, "Prefix": self.prefix}
            if token:
                kw["ContinuationToken"] = token
            page = self.client.list_objects_v2(**kw)
            for obj in page.get("Contents") or []:
                key = str(obj["Key"])
                if key.endswith("/") or key.rsplit("/", 1)[-1].startswith("."):
                    continue
                refs.append(FileRef(key, int(obj.get("Size", 0)), str(obj.get("ETag", ""))))
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or not token:
                return refs

    async def list(self) -> list[FileRef]:
        return await asyncio.to_thread(self._list)

    def _read(self, key: str) -> bytes:
        body = self.client.get_object(Bucket=self.bucket, Key=key)["Body"]
        data = body.read()
        return bytes(data)

    async def read(self, key: str) -> bytes:
        return await asyncio.to_thread(self._read, key)

    def uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def key(self, uri: str) -> str | None:
        prefix = f"s3://{self.bucket}/"
        return uri[len(prefix) :] if uri.startswith(prefix) else None


def build_source(config: Any, credentials: Any = None, *, s3_client: Any = None) -> FileSource:
    """A file source from a trigger's ``source`` (credentials already resolved)."""
    if not isinstance(config, dict):
        raise FileSourceError("a file source is an object with a type")
    kind = config.get("type")
    if kind == "folder":
        if not config.get("path"):
            raise FileSourceError("folder sources need a path")
        return FolderSource(str(config["path"]))
    if kind == "s3":
        if not config.get("bucket"):
            raise FileSourceError("s3 sources need a bucket")
        creds = credentials
        if isinstance(creds, str):
            try:
                creds = json.loads(creds)
            except ValueError:
                raise FileSourceError("s3 credentials must be JSON with access keys") from None
        if creds is not None and not (
            isinstance(creds, dict) and {"access_key_id", "secret_access_key"} <= set(creds)
        ):
            raise FileSourceError("s3 credentials need access_key_id and secret_access_key")
        return S3Source(
            str(config["bucket"]), str(config.get("prefix") or ""),
            region=config.get("region"), credentials=creds, client=s3_client,
        )  # fmt: skip
    raise FileSourceError(f"unknown file source type {kind!r} (folder or s3)")


def matches(key: str, patterns: list[str] | None) -> bool:
    if not patterns:
        return True
    name = key.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(name.lower(), p.lower()) or fnmatch.fnmatch(key, p) for p in patterns
    )


def content_type(key: str) -> str:
    return mimetypes.guess_type(key)[0] or "application/octet-stream"


def file_event(
    source: FileSource, ref: FileRef, data: bytes, sha256: str, inline_bytes: int
) -> dict[str, Any]:
    info: dict[str, Any] = {
        "key": ref.key,
        "name": ref.key.rsplit("/", 1)[-1],
        "size": len(data),
        "sha256": sha256,
        "uri": source.uri(ref.key),
        "content_type": content_type(ref.key),
    }
    if Path(ref.key).suffix.lower() in TEXT_TYPES and len(data) <= inline_bytes:
        try:
            info["text"] = data.decode("utf-8-sig")
        except UnicodeDecodeError:
            info["text"] = data.decode("latin-1")
    return {"file": info}


def dotted(value: Any, path: str) -> Any:
    """``file.sha256`` from an event; None when any part is missing."""
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value
