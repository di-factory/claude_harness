"""G5: the egress proxy enforces allow_hosts, and Python extensions run in a container."""

from __future__ import annotations

import asyncio
import base64
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.core.messages import ToolStatus, ToolUseBlock
from dif_general_harness.tools.egress import EgressProxy, host_allowed
from dif_general_harness.tools.packs import ContainerExecutor
from dif_general_harness.tools.packs.coding import CommandResult
from dif_general_harness.tools.python import PythonToolError
from dif_general_harness.tools.python_sandbox import describe
from dif_general_harness.tools.registry import Effect
from tests.support import Env


async def _upstream() -> tuple[asyncio.Server, int, list[bytes]]:
    seen: list[bytes] = []

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(await reader.readuntil(b"\r\n\r\n"))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1]), seen


def _auth(token: str) -> str:
    return "Basic " + base64.b64encode(f"dif:{token}".encode()).decode()


async def _ask(port: int, head: str, then: bytes = b"") -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(head.encode() + then)
    await writer.drain()
    data = b""
    try:
        while chunk := await asyncio.wait_for(reader.read(65536), 2):
            data += chunk
            if then and b"hello" in data:
                break
    finally:
        writer.close()
    return data


async def _proxy(decisions: list[tuple[Any, ...]]) -> EgressProxy:
    async def resolve(host: str, port: int) -> list[str]:
        table = {"api.allowed.test": ["127.0.0.1"], "internal.allowed.test": ["10.0.0.5"]}
        if host not in table:
            raise OSError("unknown host")
        return table[host]

    async def record(*decision: Any) -> None:
        decisions.append(decision)

    proxy = EgressProxy(
        "127.0.0.1:0", listen_host="127.0.0.1", listen_port=0, on_decision=record,
        resolve=resolve, address_ok=lambda ip: ip == "127.0.0.1",  # the test upstream only
    )  # fmt: skip
    await proxy.start()
    return proxy


async def test_the_proxy_lets_out_only_what_a_command_was_allowed() -> None:
    server, port, seen = await _upstream()
    decisions: list[tuple[Any, ...]] = []
    proxy = await _proxy(decisions)
    listen = int(proxy.advertise.rsplit(":", 1)[1])
    token = proxy.grant([f"api.allowed.test:{port}", f"internal.allowed.test:{port}"], "cmd1")
    auth = f"Proxy-Authorization: {_auth(token)}\r\n"
    try:
        plain = await _ask(listen, f"GET http://api.allowed.test:{port}/x?y=1 HTTP/1.1\r\n"
                                   f"Host: api.allowed.test\r\n{auth}\r\n")  # fmt: skip
        assert plain.startswith(b"HTTP/1.1 200") and plain.endswith(b"hello")
        assert seen[-1].startswith(b"GET /x?y=1 HTTP/1.1") and b"Proxy-" not in seen[-1]

        tunnel = await _ask(
            listen, f"CONNECT api.allowed.test:{port} HTTP/1.1\r\n{auth}\r\n",
            b"GET /inside HTTP/1.1\r\nHost: api\r\n\r\n",
        )  # fmt: skip
        assert tunnel.startswith(b"HTTP/1.1 200 Connection established") and b"hello" in tunnel
        assert seen[-1].startswith(b"GET /inside")

        denied = {
            "no credentials": f"CONNECT api.allowed.test:{port} HTTP/1.1\r\n\r\n",
            "wrong token": f"CONNECT api.allowed.test:{port} HTTP/1.1\r\n"
            f"Proxy-Authorization: {_auth('guess')}\r\n\r\n",
            "unlisted host": f"CONNECT evil.test:443 HTTP/1.1\r\n{auth}\r\n",
            "private address": f"CONNECT internal.allowed.test:{port} HTTP/1.1\r\n{auth}\r\n",
            "unlisted port": f"CONNECT api.allowed.test:22 HTTP/1.1\r\n{auth}\r\n",
        }
        for why, head in denied.items():
            answer = await _ask(listen, head)
            expected = b"407" if "token" in why or "credentials" in why else b"403"
            assert answer.split(b" ")[1] == expected, why
        proxy.revoke(token)
        again = await _ask(listen, f"CONNECT api.allowed.test:{port} HTTP/1.1\r\n{auth}\r\n")
        assert b" 407 " in again  # a finished command's credentials are dead
    finally:
        await proxy.close()
        server.close()
    assert [(d[1], d[3], d[4]) for d in decisions] == [
        ("api.allowed.test", True, ""),
        ("api.allowed.test", True, ""),
        ("evil.test", False, "not in allow_hosts"),
        ("internal.allowed.test", False, "resolves to a private address"),
        ("api.allowed.test", False, "not in allow_hosts"),
    ]


def test_host_patterns() -> None:
    allow = ["github.com", "*.pythonhosted.org", "registry.internal:8443"]
    assert host_allowed("GitHub.com.", 443, allow) and host_allowed("github.com", 80, allow)
    assert not host_allowed("api.github.com", 443, allow)  # exact unless a wildcard
    assert host_allowed("files.pythonhosted.org", 443, allow)
    assert not host_allowed("pythonhosted.org", 443, allow)
    assert host_allowed("registry.internal", 8443, allow)
    assert not host_allowed("registry.internal", 443, allow)


async def test_each_command_gets_its_own_short_lived_credentials(tmp_path: Path) -> None:
    decisions: list[tuple[Any, ...]] = []
    proxy = await _proxy(decisions)
    seen: dict[str, Any] = {}

    async def spawn(argv: list[str], timeout_s: float, kill: list[str]) -> CommandResult:
        url = next((a.split("=", 1)[1] for a in argv if a.startswith("HTTPS_PROXY=")), "")
        if url:
            token = url.split("dif:", 1)[1].split("@", 1)[0]
            seen["token"], seen["live"] = token, token in proxy.grants
        seen["argv"] = argv
        return CommandResult(0, "done")

    ex = ContainerExecutor(image="img", allow_hosts=["pypi.org"], egress_proxy="builtin",
                           proxy=proxy, spawn=spawn)  # fmt: skip
    try:
        await ex.run("pip install x", tmp_path, 10)
        assert seen["live"] and seen["token"] not in proxy.grants  # revoked when it ended
        joined = " ".join(seen["argv"])
        assert "--network dif-egress" in joined and proxy.advertise in joined
        first = seen["token"]
        await ex.run("pip install y", tmp_path, 10)
        assert seen["token"] != first
        closed = ContainerExecutor(image="img", egress_proxy="builtin", proxy=proxy, spawn=spawn)
        await closed.run("ls", tmp_path, 10)  # nothing allowed: no network at all
        assert "--network none" in " ".join(seen["argv"])
    finally:
        await proxy.close()


def _workspace(egress: str | None) -> Any:
    def edit(spec: dict[str, Any]) -> None:
        spec["tools"]["packs"].append("coding")
        spec["workspaces"] = {"repo": {"executor": {
            "type": "container", "image": "sandbox:1", "allow_hosts": ["pypi.org"],
            **({"egress_proxy": egress} if egress else {}),
        }}}  # fmt: skip
        spec["agents"]["ops"]["workspace"] = "repo"
        spec["agents"]["ops"]["tools"] = ["coding.*"]

    return edit


async def test_the_instance_runs_the_builtin_proxy(tmp_path: Path) -> None:
    decisions: list[tuple[Any, ...]] = []
    proxy = await _proxy(decisions)
    env = Env(tmp_path, [], edit=_workspace("builtin"))
    env.workspaces = {"repo": tmp_path}
    env.egress = proxy
    inst, _, client = await env.open()
    async with inst, client:
        assert "egress_proxy_missing" not in {i.code for i in inst.issues}
        assert inst.egress is proxy and proxy.on_decision is not None
        token = proxy.grant(["pypi.org"], "dif-exec-1")
        listen = int(proxy.advertise.rsplit(":", 1)[1])
        await _ask(listen, f"CONNECT evil.test:443 HTTP/1.1\r\n"
                           f"Proxy-Authorization: {_auth(token)}\r\n\r\n")  # fmt: skip
        [record] = await inst.audit.records(inst.scope, action="egress")
        assert (record.actor, record.subject) == ("sandbox:dif-exec-1", "evil.test:443")
        assert record.data == {"allowed": False, "reason": "not in allow_hosts"}


async def test_builtin_without_an_address_means_no_network(tmp_path: Path) -> None:
    env = Env(tmp_path, [], edit=_workspace("builtin"))
    env.workspaces = {"repo": tmp_path}
    inst, _, client = await env.open()
    async with inst, client:
        [issue] = [i for i in inst.issues if i.code == "egress_proxy_missing"]
        assert "DIF_EGRESS_ADVERTISE" in issue.message


EXT = '''
import os
from dif_general_harness.tools.registry import Effect, tool

MARK = os.environ.get("DIF_MODULE", "harness")  # the harness never runs this module


@tool("billing.charge", effect=Effect.EXTERNAL)
async def charge(amount: float, currency: str = "MXN", note: str | None = None) -> dict:
    """Charge an amount."""
    return {"charged": amount, "currency": currency, "where": MARK}


def split_bill(total: float, people: list[str]) -> dict:
    """Split a bill evenly."""
    if not people:
        raise ValueError("nobody to split with")
    return {p: round(total / len(people), 2) for p in people}
'''

FAKE_RUNTIME = """#!{python}
# A stand-in for docker run: plays the container with a local python, logs its arguments.
import json, os, subprocess, sys
args = sys.argv[1:]
with open({log!r}, "a") as f:
    f.write(json.dumps(args) + "\\n")
env, source = {{"PATH": os.environ["PATH"]}}, None
for i, a in enumerate(args):
    if a == "--env":
        k, _, v = args[i + 1].partition("=")
        env[k] = v
    if a == "--mount":
        source = dict(p.split("=", 1) for p in args[i + 1].split(",") if "=" in p)["source"]
if "DIF_RUNNER" in env:
    env["DIF_RUNNER"] = env["DIF_RUNNER"].replace("/workspace", source)
env["PYTHONPATH"] = ""
sys.exit(subprocess.run(["bash", "-c", args[-1]], env=env).returncode)
"""


def _isolated(pack: Path, runtime: Path) -> Any:
    def edit(spec: dict[str, Any]) -> None:
        (pack / "extensions").mkdir(parents=True, exist_ok=True)
        (pack / "extensions" / "billing.py").write_text(EXT)
        spec["tools"]["python"] = ["extensions.billing:charge", "extensions.billing:split_bill"]
        spec["tools"]["config"] = {"python": {
            "isolation": "container", "image": "python:3.12-slim", "runtime": str(runtime),
            "timeout": "1m", "memory": "256m",
        }}  # fmt: skip
        spec["agents"]["ops"]["tools"] = ["billing.*"]

    return edit


def test_contracts_are_read_without_running_the_code(tmp_path: Path) -> None:
    ext = tmp_path / "billing.py"
    ext.write_text(EXT)
    name, doc, schema, effect = describe(f"{ext}:charge")
    assert (name, doc, effect) == ("billing.charge", "Charge an amount.", Effect.EXTERNAL)
    assert schema["required"] == ["amount"]
    assert schema["properties"]["amount"] == {"type": "number"}
    assert schema["properties"]["note"] == {"anyOf": [{"type": "string"}, {"type": "null"}]}
    _, _, split, effect = describe(f"{ext}:split_bill")
    assert effect is Effect.READ and split["properties"]["people"]["items"] == {"type": "string"}
    with pytest.raises(PythonToolError, match="no function"):
        describe(f"{ext}:missing")


async def test_isolated_extensions_run_in_a_container(tmp_path: Path) -> None:
    log = tmp_path / "runtime.log"
    runtime = tmp_path / "fake-docker"
    runtime.write_text(FAKE_RUNTIME.format(python=sys.executable, log=str(log)))
    runtime.chmod(runtime.stat().st_mode | stat.S_IXUSR)
    env = Env(tmp_path, [], edit=_isolated(tmp_path / "packs" / "desk", runtime))
    inst, headless, client = await env.open()
    async with inst, client:
        assert not [i for i in inst.issues if i.code in ("python_tool_error", "executor_error")]
        tools = headless.agent("ops").tools
        charge = tools.get("billing.charge")
        assert charge is not None and charge.effect is Effect.EXTERNAL
        assert charge.source == "python-container"
        assert not [m for m in sys.modules if m.startswith("dif_extension_") and "billing" in m]

        split = await tools.execute(ToolUseBlock(
            id="1", name="billing.split_bill", input={"total": 90, "people": ["ana", "luis"]}
        ))  # fmt: skip
        assert split.status is ToolStatus.OK and split.content == {"ana": 45.0, "luis": 45.0}
        charged = await tools.execute(ToolUseBlock(id="2", name="billing.charge",
                                                   input={"amount": 12.5}))  # fmt: skip
        assert charged.content == {"charged": 12.5, "currency": "MXN", "where": "billing.py"}
        failed = await tools.execute(
            ToolUseBlock(id="3", name="billing.split_bill", input={"total": 1, "people": []})
        )
        assert failed.status is ToolStatus.ERROR and "nobody to split with" in (failed.error or "")
        bad = await tools.execute(
            ToolUseBlock(id="4", name="billing.split_bill", input={"total": "x"})
        )
        assert bad.status is ToolStatus.ERROR and "invalid input" in (bad.error or "")

        argv = json.loads(log.read_text().splitlines()[0])
        joined = " ".join(argv)
        assert "--network none" in joined and ",readonly" in joined
        assert "--memory 256m" in joined and "python:3.12-slim" in argv
