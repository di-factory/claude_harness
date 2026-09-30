"""The egress proxy: the only way out for sandboxed commands, and it enforces ``allow_hosts``.

Containers run on an internal network (``proxy_network``) that has no route out; the
harness joins that network too and runs this proxy. Each command gets its own credentials
(``http://dif:<token>@<advertise>``), bound to its workspace's ``allow_hosts`` and revoked
when the command ends, so one sandbox cannot use another's allowance.

For every request the proxy:

- requires the command's token (``Proxy-Authorization``), else ``407``;
- allows only listed hosts (``github.com``, ``*.pythonhosted.org``; an entry may name a
  port, ``registry.internal:8443``; otherwise ports 443 and 80);
- resolves the host itself and refuses private, loopback, link-local and metadata
  addresses, then connects to the address it checked (no DNS rebinding);
- tunnels HTTPS (``CONNECT``) without seeing its content, and forwards plain HTTP;
- reports every decision (``on_decision``), which the instance audits as ``egress``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

MAX_HEAD = 16 * 1024
DEFAULT_PORTS = (443, 80)
Decision = Callable[[str, str, int, bool, str], Awaitable[None]]
Resolve = Callable[[str, int], Awaitable[list[str]]]


def public_address(ip: str) -> bool:
    addr = ipaddress.ip_address(ip)
    return addr.is_global and not addr.is_multicast


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=1)
    return [str(info[4][0]) for info in infos]


def host_allowed(host: str, port: int, allow: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for entry in allow:
        name, _, entry_port = entry.lower().rstrip(".").partition(":")
        ports = (int(entry_port),) if entry_port.isdigit() else DEFAULT_PORTS
        if port not in ports:
            continue
        if name.startswith("*.") and host.endswith(name[1:]) and host != name[2:]:
            return True
        if host == name:
            return True
    return False


@dataclass
class Grant:
    label: str
    hosts: list[str]


class EgressProxy:
    def __init__(
        self,
        advertise: str,
        *,
        listen_host: str = "0.0.0.0",
        listen_port: int = 3128,
        on_decision: Decision | None = None,
        resolve: Resolve = _resolve,
        address_ok: Callable[[str], bool] = public_address,
    ) -> None:
        self.advertise = advertise  # host:port the containers reach the proxy at
        self.listen = (listen_host, listen_port)
        self.on_decision = on_decision
        self.resolve = resolve
        self.address_ok = address_ok
        self.grants: dict[str, Grant] = {}
        self._server: asyncio.Server | None = None

    # --- credentials -------------------------------------------------------------------

    def grant(self, hosts: list[str], label: str) -> str:
        token = secrets.token_urlsafe(24)
        self.grants[token] = Grant(label, list(hosts))
        return token

    def revoke(self, token: str) -> None:
        self.grants.pop(token, None)

    def url_for(self, token: str) -> str:
        return f"http://dif:{token}@{self.advertise}"

    # --- serving -----------------------------------------------------------------------

    async def start(self) -> int:
        self._server = await asyncio.start_server(self._handle, *self.listen)
        port = int(self._server.sockets[0].getsockname()[1])
        if self.advertise.endswith(":0"):  # tests: listen on a free port
            self.advertise = f"{self.advertise[:-2]}:{port}"
        return port

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await self._serve(reader, writer)
        except (ConnectionError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = await reader.readuntil(b"\r\n\r\n")
        if len(head) > MAX_HEAD:
            return await _reply(writer, 431, "request header too large")
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, version = lines[0].split(" ", 2)
        except ValueError:
            return await _reply(writer, 400, "bad request line")
        headers = [line.split(":", 1) for line in lines[1:] if ":" in line]
        found = {k.strip().lower(): v.strip() for k, v in headers}
        grant = self._grant(found.get("proxy-authorization", ""))
        if grant is None:
            return await _reply(writer, 407, "proxy credentials required",
                                {"Proxy-Authenticate": 'Basic realm="dif"'})  # fmt: skip
        if method.upper() == "CONNECT":
            host, _, port_text = target.rpartition(":")
            host, port = host.strip("[]"), int(port_text) if port_text.isdigit() else 443
        else:
            parts = urlsplit(target)
            if parts.scheme != "http" or not parts.hostname:
                return await _reply(writer, 400, "use CONNECT for https, absolute URLs for http")
            host, port = parts.hostname, parts.port or 80
        address = await self._decide(grant, host, port)
        if address is None:
            return await _reply(writer, 403, f"{host}:{port} is not allowed")
        upstream_reader, upstream_writer = await asyncio.open_connection(address, port)
        try:
            if method.upper() == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
            else:
                parts = urlsplit(target)
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                kept = [f"{k.strip()}:{v}" for k, v in headers
                        if not k.strip().lower().startswith("proxy-")
                        and k.strip().lower() != "connection"]  # fmt: skip
                request = [f"{method} {path} {version}", *kept, "Connection: close", "", ""]
                upstream_writer.write("\r\n".join(request).encode("latin-1"))
                await upstream_writer.drain()
            await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))
        finally:
            with contextlib.suppress(Exception):
                upstream_writer.close()

    def _grant(self, auth: str) -> Grant | None:
        scheme, _, value = auth.partition(" ")
        if scheme.lower() != "basic":
            return None
        try:
            user, _, token = base64.b64decode(value).decode().partition(":")
        except ValueError:
            return None
        return self.grants.get(token) if user == "dif" else None

    async def _decide(self, grant: Grant, host: str, port: int) -> str | None:
        reason, address = "", None
        if not host_allowed(host, port, grant.hosts):
            reason = "not in allow_hosts"
        else:
            try:
                addresses = await self.resolve(host, port)
            except OSError as exc:
                addresses, reason = [], f"cannot resolve: {exc}"
            if addresses and all(self.address_ok(a) for a in addresses):
                address = addresses[0]
            elif addresses:
                reason = "resolves to a private address"
        if self.on_decision is not None:
            await self.on_decision(grant.label, host, port, address is not None, reason)
        return address


async def _reply(
    writer: asyncio.StreamWriter, status: int, text: str, extra: dict[str, str] | None = None
) -> None:
    reasons = {400: "Bad Request", 403: "Forbidden", 407: "Proxy Authentication Required",
               431: "Request Header Fields Too Large"}  # fmt: skip
    body = (text + "\n").encode()
    head = [
        f"HTTP/1.1 {status} {reasons.get(status, 'Error')}",
        "Content-Type: text/plain",
        f"Content-Length: {len(body)}",
        "Connection: close",
    ]
    head += [f"{k}: {v}" for k, v in (extra or {}).items()]
    writer.write(("\r\n".join(head) + "\r\n\r\n").encode() + body)
    await writer.drain()


async def _pipe(source: asyncio.StreamReader, sink: asyncio.StreamWriter) -> None:
    try:
        while data := await source.read(65536):
            sink.write(data)
            await sink.drain()
    finally:
        with contextlib.suppress(Exception):
            if sink.can_write_eof():
                sink.write_eof()
