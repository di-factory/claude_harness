"""Tool sources (M1.3): HTTP connectors, MCP servers and the built-in packs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from mcp.server.mcpserver import MCPServer
from mcp_types import ToolAnnotations

from dif_general_harness.core.messages import ToolResultBlock, ToolStatus, ToolUseBlock
from dif_general_harness.core.scope import Scope
from dif_general_harness.spec import load_pack
from dif_general_harness.tools import Effect, ToolRegistry
from dif_general_harness.tools.http import ConnectorError, http_tools, input_schema
from dif_general_harness.tools.mcp import McpToolSource
from dif_general_harness.tools.packs import NoteStore, Workspace, coding_tools, general_tools
from dif_general_harness.tools.packs.coding import CommandResult, is_secret_path
from dif_general_harness.tools.packs.general import BlockedUrl, check_public_url


async def _exec(registry: ToolRegistry, name: str, **args: Any) -> ToolResultBlock:
    return await registry.execute(ToolUseBlock(id="t", name=name, input=args))


# --- HTTP connectors ---------------------------------------------------------------


class Recorder:
    def __init__(self, status: int = 200, body: Any = None) -> None:
        self.requests: list[httpx2.Request] = []
        self.status = status
        self.body = {"ok": True} if body is None else body

    def client(self) -> httpx2.AsyncClient:
        def handle(request: httpx2.Request) -> httpx2.Response:
            self.requests.append(request)
            if isinstance(self.body, str):
                return httpx2.Response(self.status, text=self.body)
            return httpx2.Response(self.status, json=self.body)

        return httpx2.AsyncClient(transport=httpx2.MockTransport(handle))


def _identity(auth: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "base_url": "https://idp.example/api/",
        "auth": auth or {"type": "bearer", "token": "tok-123"},
        "operations": {
            "lookup_user": {
                "method": "GET",
                "path": "/users?email={email}",
                "effect": "read",
                "input": {"email": "string", "active": "boolean?"},
            },
            "reset_password": {
                "method": "POST",
                "path": "/users/{user_id}/password/reset",
                "effect": "external",
                "input": {"user_id": "string", "notify": "boolean?"},
                "verify": "identity-verified",
            },
        },
    }


def test_input_short_form() -> None:
    schema = input_schema({"a": "string", "b": "number?", "c": {"type": "array"}}, "/x/{a}")
    assert schema["properties"] == {
        "a": {"type": "string"},
        "b": {"type": "number"},
        "c": {"type": "array"},
    }
    assert schema["required"] == ["a", "c"] and schema["additionalProperties"] is False
    with pytest.raises(ConnectorError, match="unknown type"):
        input_schema({"a": "str"}, "/")
    with pytest.raises(ConnectorError, match="not in the input"):
        input_schema({}, "/users/{user_id}")


async def test_http_connector_requests() -> None:
    wire = Recorder()
    tools = http_tools("identity", _identity(), client=wire.client())
    by_name = {t.name: t for t in tools}
    assert by_name["identity.reset_password"].effect is Effect.EXTERNAL
    assert by_name["identity.reset_password"].verify == "identity-verified"
    assert by_name["identity.lookup_user"].effect is Effect.READ
    registry = ToolRegistry(tools)

    result = await _exec(registry, "identity.lookup_user", email="a+b@x.mx", active=True)
    assert result.status is ToolStatus.OK and result.content == {"ok": True}
    get = wire.requests[-1]
    assert get.method == "GET" and get.headers["authorization"] == "Bearer tok-123"
    assert get.url.path == "/api/users"
    assert dict(get.url.params) == {"email": "a+b@x.mx", "active": "true"}

    await _exec(registry, "identity.reset_password", user_id="../admin", notify=False)
    post = wire.requests[-1]
    assert post.method == "POST"
    assert post.url.raw_path.decode() == "/api/users/..%2Fadmin/password/reset"
    assert json.loads(post.content) == {"notify": False}


async def test_http_connector_observations() -> None:
    registry = ToolRegistry(
        http_tools("identity", _identity(), client=Recorder(404, "no").client())
    )
    missing = await _exec(registry, "identity.reset_password", user_id="u1")
    assert missing.status is ToolStatus.ERROR and "HTTP 404" in (missing.error or "")
    invalid = await _exec(registry, "identity.reset_password", user_id=7)
    assert invalid.status is ToolStatus.ERROR and "invalid input" in (invalid.error or "")
    extra = await _exec(registry, "identity.reset_password", user_id="u", admin=True)
    assert extra.status is ToolStatus.ERROR

    text = ToolRegistry(
        http_tools("identity", _identity(), client=Recorder(200, "x" * 30_000).client())
    )
    long = await _exec(text, "identity.lookup_user", email="e")
    assert isinstance(long.content, str) and "truncated" in long.content


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"base_url": "http://idp.example"}, "must be https"),
        ({"auth": {"type": "aws_role", "role": "r"}}, "not supported"),
        ({"auth": {"type": "bearer", "token": {"$secret": "idp"}}}, "resolved"),
    ],
)
def test_http_connector_refuses_unsafe_config(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ConnectorError, match=message):
        http_tools("identity", {**_identity(), **change})


def test_http_connector_basic_auth_and_localhost() -> None:
    tools = http_tools(
        "local",
        {
            **_identity({"type": "basic", "username": "u", "password": "p"}),
            "base_url": "http://localhost:8080",
        },
    )
    assert len(tools) == 2


def test_example_connectors_build(examples: Path) -> None:
    spec = load_pack(examples / "service-desk-cell").spec
    for name, connector in spec.tools.http.items():
        conn = connector.model_dump()
        conn["base_url"] = "https://resolved.example"
        conn["auth"] = {"type": "bearer", "token": "resolved"}
        assert http_tools(name, conn)


# --- MCP ---------------------------------------------------------------------------


def _helpdesk() -> MCPServer:
    server = MCPServer("helpdesk")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def get_ticket(ticket_id: str) -> str:
        """Fetch a ticket."""
        return f"{ticket_id}: printer on fire"

    @server.tool(name="close-ticket")
    def close_ticket(ticket_id: str, note: str = "") -> dict[str, str]:
        """Close a ticket."""
        return {"id": ticket_id, "state": "closed"}

    @server.tool()
    def broken() -> str:
        """Always fails."""
        raise ValueError("database down")

    return server


async def test_mcp_tools(caplog: pytest.LogCaptureFixture) -> None:
    async with McpToolSource("helpdesk", _helpdesk()) as source:
        registry = ToolRegistry(source.tools)
        assert registry.names() == [
            "helpdesk.broken",
            "helpdesk.close-ticket",
            "helpdesk.get_ticket",
        ]
        assert registry.get("helpdesk.get_ticket").effect is Effect.READ  # type: ignore[union-attr]
        assert registry.get("helpdesk.close-ticket").effect is Effect.EXTERNAL  # type: ignore[union-attr]

        ok = await _exec(registry, "helpdesk.get_ticket", ticket_id="T-1")
        assert ok.status is ToolStatus.OK and "printer on fire" in json.dumps(ok.content)
        closed = await _exec(registry, "helpdesk.close-ticket", ticket_id="T-1")
        assert closed.content == {"id": "T-1", "state": "closed"}
        failed = await _exec(registry, "helpdesk.broken")
        assert failed.status is ToolStatus.ERROR
        invalid = await _exec(registry, "helpdesk.get_ticket", ticket_id=3)
        assert invalid.status is ToolStatus.ERROR and "invalid input" in (invalid.error or "")

    after = await _exec(registry, "helpdesk.get_ticket", ticket_id="T-1")
    assert after.status is ToolStatus.ERROR and "not connected" in (after.error or "")


# --- coding pack -------------------------------------------------------------------


class FakeExecutor:
    def __init__(self) -> None:
        self.commands: list[tuple[str, Path]] = []

    async def run(self, command: str, cwd: Path, timeout_s: float) -> CommandResult:
        self.commands.append((command, cwd))
        return CommandResult(0, "ran")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def main():\n    return 'hola'\n")
    (root / "src" / "secrets.py").write_text("# code about secrets, not a secret\n")
    (root / ".env").write_text("API_KEY=planted\n")
    (root / ".env.example").write_text("API_KEY=\n")
    (root / "deploy.pem").write_text("-----BEGIN PRIVATE KEY-----\n")
    (root / ".ssh").mkdir()
    (root / ".ssh" / "config").write_text("Host x\n")
    (tmp_path / "outside.txt").write_text("outside")
    return root


def test_secret_paths() -> None:
    for secret in [
        ".env",
        ".env.prod",
        "prod.env",
        "id_rsa",
        "a/b/key.pem",
        ".ssh/config",
        "credentials.json",
        "terraform.tfstate",
    ]:
        assert is_secret_path(Path(secret)), secret
    for fine in [".env.example", "src/secrets.py", "README.md", "credentials_test.py"]:
        assert not is_secret_path(Path(fine)), fine


async def test_coding_read_write_edit_undo(repo: Path) -> None:
    ws = Workspace(repo)
    registry = ToolRegistry(coding_tools(ws, FakeExecutor()))
    read = await _exec(registry, "coding.read", path="src/app.py")
    assert read.content == "     1\tdef main():\n     2\t    return 'hola'"

    await _exec(registry, "coding.edit", path="src/app.py", old="'hola'", new="'hello'")
    await _exec(registry, "coding.write", path="src/new/mod.py", content="x = 1\n")
    assert "'hello'" in (repo / "src" / "app.py").read_text()
    assert (repo / "src" / "new" / "mod.py").exists()

    ambiguous = await _exec(registry, "coding.edit", path="src/app.py", old="e", new="E")
    assert ambiguous.status is ToolStatus.ERROR and "matches" in (ambiguous.error or "")
    missing = await _exec(registry, "coding.edit", path="src/app.py", old="nope", new="x")
    assert missing.status is ToolStatus.ERROR

    assert ws.undo() is not None and not (repo / "src" / "new" / "mod.py").exists()
    assert ws.undo() is not None and "'hola'" in (repo / "src" / "app.py").read_text()
    assert ws.undo() is None


async def test_coding_confinement(repo: Path) -> None:
    registry = ToolRegistry(coding_tools(Workspace(repo), FakeExecutor()))
    (repo / "link").symlink_to(repo.parent / "outside.txt")
    for path in ["../outside.txt", "/etc/passwd", "link", ".env", ".ssh/config", "deploy.pem"]:
        result = await _exec(registry, "coding.read", path=path)
        assert result.status is ToolStatus.ERROR, path
    wrote = await _exec(registry, "coding.write", path=".env", content="x")
    assert wrote.status is ToolStatus.ERROR and (repo / ".env").read_text() == "API_KEY=planted\n"
    assert (await _exec(registry, "coding.read", path=".env.example")).status is ToolStatus.OK
    assert (await _exec(registry, "coding.glob", pattern="../*")).status is ToolStatus.ERROR


async def test_coding_glob_and_grep_hide_secrets(repo: Path) -> None:
    registry = ToolRegistry(coding_tools(Workspace(repo), FakeExecutor()))
    listed = (await _exec(registry, "coding.glob", pattern="**/*")).content
    assert listed == [".env.example", "src/app.py", "src/secrets.py"]
    grep = (await _exec(registry, "coding.grep", pattern="API_KEY|hola")).content
    assert grep == [".env.example:1: API_KEY=", "src/app.py:2:     return 'hola'"]


async def test_coding_bash(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeExecutor()
    registry = ToolRegistry(coding_tools(Workspace(repo), fake))
    assert registry.get("coding.bash").effect is Effect.EXTERNAL  # type: ignore[union-attr]
    result = await _exec(registry, "coding.bash", command="make test")
    assert result.content == {"exit_code": 0, "output": "ran"}
    assert fake.commands == [("make test", repo.resolve())]

    # the real subprocess executor: workspace cwd, no harness secrets in the environment
    monkeypatch.setenv("DIF_SECRET_LLM", "planted-secret")
    real = ToolRegistry(coding_tools(Workspace(repo), bash_timeout_s=0.5))
    run = await _exec(real, "coding.bash", command='pwd; echo "[$DIF_SECRET_LLM]"; exit 3')
    assert run.content == {"exit_code": 3, "output": f"{repo.resolve()}\n[]\n"}
    slow = await _exec(real, "coding.bash", command="sleep 5")
    assert slow.status is ToolStatus.TIMEOUT


# --- general pack ------------------------------------------------------------------


async def test_notes(tmp_path: Path, scope: Scope) -> None:
    registry = ToolRegistry(general_tools(NoteStore(tmp_path, scope)))
    await _exec(registry, "notes.write", key="horario", text="L-V 9 a 18")
    assert (await _exec(registry, "notes.read", key="horario")).content == "L-V 9 a 18"
    assert (await _exec(registry, "notes.list")).content == ["horario"]
    assert (await _exec(registry, "notes.read", key="nope")).status is ToolStatus.ERROR
    assert (await _exec(registry, "notes.write", key="../x", text="")).status is ToolStatus.ERROR
    assert (tmp_path / scope.tenant_id / scope.instance_id / "notes.json").exists()
    other = NoteStore(tmp_path, Scope(tenant_id="other", instance_id="appointments"))
    assert other.keys() == []


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/admin",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.5/",
        "http://[::1]/",
        "http://localhost:8080/",
        "file:///etc/passwd",
    ],
)
async def test_http_get_blocks_private_addresses(url: str) -> None:
    with pytest.raises(BlockedUrl):
        await check_public_url(httpx2.URL(url))


async def test_http_get_checks_every_hop(tmp_path: Path, scope: Scope) -> None:
    def handle(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "public.example":
            return httpx2.Response(302, headers={"location": "http://internal.example/secret"})
        return httpx2.Response(200, text="internal data")

    async def fake_check(url: httpx2.URL) -> None:
        if url.host != "public.example":
            raise BlockedUrl(f"{url.host} is private")

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handle), follow_redirects=True)
    registry = ToolRegistry(
        general_tools(NoteStore(tmp_path, scope), client=client, url_check=fake_check)
    )
    result = await _exec(registry, "http.get", url="https://public.example/page")
    assert result.status is ToolStatus.ERROR and "private" in (result.error or "")
