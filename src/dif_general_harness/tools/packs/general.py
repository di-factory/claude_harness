"""The ``general`` tool pack: web reads and durable notes.

- ``http.get`` fetches a public URL. Private, loopback, link-local and metadata addresses are
  refused on every hop (redirects included), so a prompt cannot turn the agent into a proxy
  into the client's network. Web search needs a search provider and arrives with it.
- ``notes.*`` keep short notes per instance (``<root>/<tenant>/<instance>/notes.json``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
from pathlib import Path
from typing import Any

import httpx2

from ...core.scope import Scope
from ..registry import Effect, Tool, tool

MAX_BODY = 50_000
_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,79}$")


class BlockedUrl(ValueError):
    pass


async def check_public_url(url: httpx2.URL) -> None:
    if url.scheme not in {"http", "https"}:
        raise BlockedUrl(f"scheme {url.scheme!r} is not allowed")
    host = url.host
    if not host:
        raise BlockedUrl("URL has no host")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        infos = await asyncio.get_running_loop().getaddrinfo(host, url.port or 443)
        addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
    for addr in addresses:
        if not addr.is_global or addr.is_multicast:
            raise BlockedUrl(f"{host} resolves to a non-public address")


class NoteStore:
    def __init__(self, root: Path | str, scope: Scope) -> None:
        self.path = Path(root) / scope.tenant_id / scope.instance_id / "notes.json"

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        data: dict[str, str] = json.loads(self.path.read_text(encoding="utf-8"))
        return data

    def put(self, key: str, text: str) -> None:
        if not _KEY.match(key):
            raise ValueError(f"invalid note key {key!r}")
        notes = self._load()
        notes[key] = text
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(notes, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def get(self, key: str) -> str | None:
        return self._load().get(key)

    def keys(self) -> list[str]:
        return sorted(self._load())


def general_tools(
    notes: NoteStore,
    *,
    client: httpx2.AsyncClient | None = None,
    url_check: Any = check_public_url,
) -> list[Tool]:
    async def guard(request: httpx2.Request) -> None:
        await url_check(request.url)

    http = client or httpx2.AsyncClient(timeout=20.0, follow_redirects=True, max_redirects=5)
    hooks = http.event_hooks
    http.event_hooks = {**hooks, "request": [*hooks.get("request", []), guard]}

    @tool("http.get")
    async def http_get(url: str) -> dict[str, Any]:
        """Fetch a public web page or API with GET. Returns status, content type and body."""
        response = await http.get(url, headers={"User-Agent": "dif-general-harness"})
        body = response.text
        if len(body) > MAX_BODY:
            body = body[:MAX_BODY] + f"\n[truncated {len(body) - MAX_BODY} chars]"
        return {
            "status": response.status_code,
            "content_type": response.headers.get("content-type", ""),
            "url": str(response.url),
            "body": body,
        }

    @tool("notes.write", effect=Effect.WRITE)
    async def notes_write(key: str, text: str) -> str:
        """Save a note under a short key, replacing any earlier note with that key."""
        notes.put(key, text)
        return f"saved note {key!r}"

    @tool("notes.read")
    async def notes_read(key: str) -> str:
        """Read a note by key."""
        value = notes.get(key)
        if value is None:
            raise KeyError(f"no note {key!r}")
        return value

    @tool("notes.list")
    async def notes_list() -> list[str]:
        """List note keys."""
        return notes.keys()

    return [http_get, notes_write, notes_read, notes_list]
