"""The Dev Cell test kit (docs/testing/dev-cell), end to end and offline: a signed GitHub
"labeled" webhook starts the workflow; the developer reads the issue, fixes the sample repo,
runs its tests, pushes to a dev-cell/ branch and opens a pull request through a fake GitHub
MCP server with GitHub's real tool names. Pushing to main is denied; the push and the pull
request only go through after the repository's own tests pass."""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from dif_general_harness.core.messages import Message, ToolStatus
from dif_general_harness.providers import FakeProvider
from dif_general_harness.providers.base import ModelRequest
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.service.app import create_app
from dif_general_harness.service.headless import Headless
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.tenancy.secrets import EnvSecrets
from dif_general_harness.tools.packs.coding import SubprocessExecutor
from dif_general_harness.tools.registry import Effect

from .conftest import EXAMPLES
from .support import calls

KIT = EXAMPLES.parent.parent / "testing" / "dev-cell"
HOOK_SECRET, ADMIN = "hook-secret-123", "admin-456"
OWNER, REPO = "acme", "devcell-test"


def _github(log: list[tuple[str, dict[str, Any]]]) -> MCPServer:
    """GitHub's MCP server, as far as the Dev Cell uses it (same tool names and arguments)."""
    server = MCPServer("github")

    @server.tool(annotations=ToolAnnotations(read_only_hint=True))
    def issue_read(method: str, owner: str, repo: str, issue_number: int) -> dict[str, Any]:
        """Read an issue."""
        log.append(("issue_read", {"issue_number": issue_number}))
        return {"number": issue_number, "title": "Page 1 skips the first records",
                "body": (KIT / "issues" / "01-off-by-one.md").read_text()}  # fmt: skip

    @server.tool()
    def add_issue_comment(owner: str, repo: str, issue_number: int, body: str) -> str:
        """Comment on an issue."""
        log.append(("add_issue_comment", {"issue_number": issue_number, "body": body}))
        return "commented"

    @server.tool()
    def create_branch(owner: str, repo: str, branch: str, from_branch: str) -> str:
        """Create a branch."""
        log.append(("create_branch", {"branch": branch, "from_branch": from_branch}))
        return f"created {branch}"

    @server.tool()
    def push_files(owner: str, repo: str, branch: str, files: list[dict[str, str]],
                   message: str) -> str:  # fmt: skip
        """Push files to a branch in one commit."""
        log.append(("push_files", {"branch": branch, "files": files}))
        return f"pushed {len(files)} file(s) to {branch}"

    @server.tool()
    def create_pull_request(owner: str, repo: str, title: str, head: str, base: str,
                            body: str) -> dict[str, Any]:  # fmt: skip
        """Open a pull request."""
        log.append(("create_pull_request", {"head": head, "base": base, "title": title}))
        return {"number": 2, "html_url": f"https://github.com/{owner}/{repo}/pull/2"}

    @server.tool()
    def merge_pull_request(owner: str, repo: str, pull_number: int) -> str:
        """Merge a pull request."""
        log.append(("merge_pull_request", {}))
        return "merged"

    return server


def _developer(fixed: str) -> Iterator[Message]:
    """The developer's turns, as a model would take them."""
    repo = {"owner": OWNER, "repo": REPO}
    yield calls(("t1", "github.issue_read", {**repo, "method": "get", "issue_number": 1}),
                ("t2", "coding.read", {"path": "pagination.py"}))  # fmt: skip
    yield calls(
        ("t3", "coding.edit", {"path": "pagination.py", "old": "start = number * size",
                               "new": "start = (number - 1) * size"}),
        ("t4", "coding.edit", {"path": "test_pagination.py",
                               "old": "class PageTest(unittest.TestCase):\n",
                               "new": "class PageTest(unittest.TestCase):\n"
                                      "    def test_first_page_starts_at_the_first_item(self)"
                                      " -> None:\n        self.assertEqual(page(list(range(25)),"
                                      " 1), list(range(10)))\n\n"}),
    )  # fmt: skip
    yield calls(("t5", "coding.bash", {"command": "python3 -m unittest -v"}))
    files = [{"path": "pagination.py", "content": fixed}]
    yield calls(
        ("t6", "github.create_branch", {**repo, "branch": "dev-cell/issue-1",
                                        "from_branch": "main"}),
        ("t7", "github.push_files", {**repo, "branch": "main", "files": files,
                                     "message": "Fix page start"}),  # never to main
    )  # fmt: skip
    yield calls(
        (
            "t8",
            "github.push_files",
            {**repo, "branch": "dev-cell/issue-1", "files": files, "message": "Fix page start"},
        )
    )
    yield calls(
        (
            "t9",
            "github.create_pull_request",
            {
                **repo,
                "title": "Fix page start (#1)",
                "head": "dev-cell/issue-1",
                "base": "main",
                "body": "Page 1 now starts at the first item, with a new test. Closes #1",
            },
        )
    )
    yield Message.assistant(
        json.dumps({"outcome": "pr_opened", "pr": f"https://github.com/{OWNER}/{REPO}/pull/2"})
    )


def _event(label: str = "agent") -> bytes:
    return json.dumps({
        "action": "labeled", "label": {"name": label},
        "issue": {"number": 1, "title": "Page 1 skips the first records"},
        "repository": {"name": REPO, "owner": {"login": OWNER}, "default_branch": "main"},
    }).encode()  # fmt: skip


def _signed(body: bytes) -> dict[str, str]:
    mac = hmac.new(HOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"content-type": "application/json", "x-hub-signature-256": f"sha256={mac}",
            "x-github-delivery": hashlib.sha256(body).hexdigest()[:16]}  # fmt: skip


async def _open(tmp_path: Path, script: Iterator[Message], log: list[Any]) -> tuple[Any, ...]:
    workspace = tmp_path / "devcell-test"
    shutil.copytree(KIT / "sample-repo", workspace)
    judged: list[ModelRequest] = []

    def model(request: ModelRequest) -> Message:
        if request.model_role == "verifier":
            judged.append(request)
            return Message.assistant('{"pass": true, "reason": "fix and test match the issue"}')
        if request.model_role == "main":
            return next(script)
        return Message.assistant('{"facts": []}')  # memory extraction, compaction

    resolved = load_instance(KIT / "devcell-test.json", PackCatalog(roots=[EXAMPLES]))
    assert resolved.ok, [str(i) for i in resolved.issues if i.severity == "error"]
    options = RuntimeOptions(
        state_root=tmp_path / "state",
        secrets=EnvSecrets({"DIF_SECRET_GITHUB_WEBHOOK": HOOK_SECRET}),
        provider=FakeProvider([model] * 40),
        mcp_servers={"github": _github(log)},
        workspaces={"repo": workspace},
        executor=SubprocessExecutor(),
    )
    inst = await Instance.open(resolved, options)
    headless = await Headless.build(inst)
    app = create_app(headless, admin_token=ADMIN, run_worker=False)
    client = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://h")
    return inst, headless, client, workspace, judged


async def test_a_labelled_issue_becomes_a_tested_pull_request(tmp_path: Path) -> None:
    fixed = (
        (KIT / "sample-repo" / "pagination.py")
        .read_text()
        .replace("start = number * size", "start = (number - 1) * size")
    )
    log: list[tuple[str, dict[str, Any]]] = []
    inst, headless, client, workspace, judged = await _open(tmp_path, _developer(fixed), log)
    async with inst, client:
        body = _event()
        r = await client.post("/hooks/github", content=body, headers=_signed(body))
        assert r.status_code == 202, r.text
        await headless.worker().drain()

        [run] = await headless.engine.runs("work-issue")
        assert run.status == "done" and run.outcome == "completed", run.error
        assert run.input == {"owner": OWNER, "repo": REPO, "issue": 1,
                             "title": "Page 1 skips the first records",
                             "default_branch": "main"}  # fmt: skip
        names = [name for name, _ in log]
        assert names == [
            "add_issue_comment",
            "issue_read",
            "create_branch",
            "push_files",
            "create_pull_request",
        ]  # the push to main never reached GitHub
        assert log[0][1] == {"issue_number": 1, "body": "Picked up by the Dev Cell."}
        assert log[3][1]["branch"] == "dev-cell/issue-1"
        assert log[4][1] == {"head": "dev-cell/issue-1", "base": "main",
                             "title": "Fix page start (#1)"}  # fmt: skip
        assert "start = (number - 1) * size" in (workspace / "pagination.py").read_text()
        assert "test_first_page_starts_at_the_first_item" in (
            workspace / "test_pagination.py").read_text()  # fmt: skip
        assert judged, "the pull request was reviewed by the verifier model first"

        session = await inst.store.load(inst.scope, run.state["sessions"]["developer"])
        results = {b.tool_use_id: b for m in session.messages for b in m.content
                   if getattr(b, "tool_use_id", None)}  # fmt: skip
        assert results["t5"].status is ToolStatus.OK and "OK" in json.dumps(results["t5"].content)
        assert results["t7"].status is ToolStatus.DENIED  # never to the default branch
        checks = await inst.audit.records(inst.scope, action="verification")
        assert {(r.subject, r.data["check"], r.data["passed"]) for r in checks} >= {
            ("github.push_files", "tests-pass", True),
            ("github.create_pull_request", "tests-pass", True),
            ("github.create_pull_request", "__verifier__", True)}  # fmt: skip


async def test_only_the_pickup_label_starts_the_cell(tmp_path: Path) -> None:
    log: list[tuple[str, dict[str, Any]]] = []
    inst, headless, client, *_ = await _open(tmp_path, iter([]), log)
    async with inst, client:
        body = _event(label="question")
        r = await client.post("/hooks/github", content=body, headers=_signed(body))
        assert r.status_code == 202
        bad = await client.post(
            "/hooks/github",
            content=body,
            headers={**_signed(body), "x-hub-signature-256": "sha256=0"},
        )
        assert bad.status_code == 401  # an unsigned event is refused
        ping = json.dumps({"zen": "Keep it simple.", "hook_id": 1}).encode()
        r = await client.post("/hooks/github", content=ping, headers=_signed(ping))
        assert r.status_code == 202, r.text  # GitHub's first delivery starts nothing
        await headless.worker().drain()
        assert await headless.engine.runs("work-issue") == [] and log == []


async def test_branch_rules_for_pushes(tmp_path: Path) -> None:
    inst, _, client, *_ = await _open(tmp_path, iter([]), [])
    async with inst, client:
        decide = inst.policy.decide
        push = "github.push_files"
        assert decide(push, Effect.EXTERNAL, {"branch": "dev-cell/issue-7"}).verdict == "allow"
        assert decide(push, Effect.EXTERNAL, {"branch": "main"}).verdict == "deny"
        assert decide(push, Effect.EXTERNAL, {"branch": "release/2.0"}).verdict == "ask"
        assert decide("github.merge_pull_request", Effect.EXTERNAL, {}).verdict == "deny"
        assert inst.tools.get("github.create_pull_request") is not None
        assert inst.tools.get(push).verify == "tests-pass"  # type: ignore[union-attr]
