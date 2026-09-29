"""MCP servers as tool sources (``tools.mcp`` in the spec).

A source connects to one server (stdio subprocess or Streamable HTTP), lists its tools and
wraps each one as a harness ``Tool`` named ``<server>.<tool>``. The MCP SDK stays inside this
module: the loop only sees ordinary tools and structured observations.

Effects: a tool the server marks ``readOnlyHint`` is ``read``; everything else is
``external``, since an MCP server acts outside the harness. ``tools.overrides`` can change
that per tool.

Lifecycle: use ``async with McpToolSource(...)``. It must be entered and exited in the same
task (the SDK uses anyio task groups); calls may come from any task.
"""

from __future__ import annotations

import re
from contextlib import AsyncExitStack
from typing import Any

import httpx2
import mcp_types
from mcp import Client, StdioServerParameters
from mcp.client.streamable_http import streamable_http_client
from mcp.server.mcpserver import MCPServer

from ..spec.schema import McpServer
from .registry import Effect, Tool, schema_check

_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


class McpToolError(RuntimeError):
    pass


def _auth_headers(auth: Any) -> dict[str, str]:
    if auth is None:
        return {}
    if isinstance(auth, str):
        return {"Authorization": f"Bearer {auth}"}
    if isinstance(auth, dict) and auth.get("type", "bearer") == "bearer" and "token" in auth:
        return {"Authorization": f"Bearer {auth['token']}"}
    if isinstance(auth, dict) and auth.get("type") == "header":
        return {str(auth["name"]): str(auth["value"])}
    raise ValueError("MCP auth must be a resolved token or {type: bearer|header, ...}")


class McpToolSource:
    def __init__(
        self,
        name: str,
        server: McpServer | dict[str, Any] | MCPServer,
        *,
        timeout_s: float = 60.0,
        env: dict[str, str] | None = None,
    ) -> None:
        self.name = name
        self.timeout_s = timeout_s
        self._server = server
        self._env = env
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self.tools: list[Tool] = []

    def _target(self, stack: AsyncExitStack) -> Any:
        if isinstance(self._server, MCPServer):  # in-process, for tests and built-in servers
            return self._server
        spec = self._server if isinstance(self._server, McpServer) else McpServer(**self._server)
        if spec.transport == "stdio":
            if not spec.command:
                raise ValueError(f"MCP server {self.name!r}: stdio needs a command")
            return StdioServerParameters(
                command=spec.command[0], args=spec.command[1:], env=self._env
            )
        if not spec.url:
            raise ValueError(f"MCP server {self.name!r}: http needs a url")
        http = httpx2.AsyncClient(headers=_auth_headers(spec.auth), timeout=self.timeout_s)
        stack.push_async_callback(http.aclose)
        return streamable_http_client(spec.url, http_client=http)

    async def __aenter__(self) -> McpToolSource:
        stack = AsyncExitStack()
        try:
            client = Client(self._target(stack), read_timeout_seconds=self.timeout_s)
            self._client = await stack.enter_async_context(client)
            listed: list[mcp_types.Tool] = []
            cursor: str | None = None
            while True:
                page = await self._client.list_tools(cursor=cursor)
                listed.extend(page.tools)
                cursor = page.next_cursor
                if not cursor:
                    break
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self.tools = [self._wrap(t) for t in listed]
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack = self._client = None

    def _wrap(self, remote: mcp_types.Tool) -> Tool:
        schema = dict(remote.input_schema or {"type": "object"})
        schema.setdefault("type", "object")
        read_only = bool(remote.annotations and remote.annotations.read_only_hint)

        async def handler(**args: Any) -> Any:
            if self._client is None:
                raise McpToolError(f"MCP server {self.name!r} is not connected")
            result = await self._client.call_tool(remote.name, args)
            return _observation(result)

        return Tool(
            name=f"{self.name}.{_UNSAFE.sub('_', remote.name)}",
            description=remote.description or remote.title or remote.name,
            input_schema=schema,
            handler=handler,
            effect=Effect.READ if read_only else Effect.EXTERNAL,
            timeout_s=self.timeout_s,
            check_input=schema_check(schema),
            source=f"mcp:{self.name}",
        )


def _observation(result: mcp_types.CallToolResult) -> Any:
    parts: list[str] = []
    for block in result.content:
        if isinstance(block, mcp_types.TextContent):
            parts.append(block.text)
        else:
            parts.append(f"[{block.type} content omitted]")
    text = "\n".join(parts)
    if result.is_error:
        raise McpToolError(text or "the MCP tool reported an error")
    if result.structured_content is not None:
        return result.structured_content
    return text
