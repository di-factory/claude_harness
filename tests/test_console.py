"""The TUI console (M1.5), driven headlessly with Textual's pilot."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from dif_general_harness.console import ApprovalScreen, ConsoleApp, ConsoleApprover
from dif_general_harness.core.messages import Message, Role, ToolUseBlock
from dif_general_harness.providers import FakeProvider
from dif_general_harness.runtime import Instance, RuntimeOptions
from dif_general_harness.spec import PackCatalog, load_instance
from dif_general_harness.tenancy import EnvSecrets


def _calls(*calls: tuple[str, str, dict[str, Any]]) -> Message:
    return Message(
        role=Role.ASSISTANT,
        content=[ToolUseBlock(id=i, name=n, input=a) for i, n, a in calls],
    )


async def _open(
    tmp_path: Path, instance_file: Path, script: list[Any]
) -> tuple[Instance, ConsoleApprover, Path]:
    resolved = load_instance(instance_file, PackCatalog(roots=[tmp_path / "packs"]))
    assert resolved.ok, resolved.issues
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "app.py").write_text("print('hola')\n")
    approver = ConsoleApprover()
    options = RuntimeOptions(
        state_root=tmp_path / "state",
        secrets=EnvSecrets({}),
        approver=approver,
        workspaces={"repo": repo},
        provider=FakeProvider(script),
    )
    return await Instance.open(resolved, options), approver, repo


async def _say(app: ConsoleApp, pilot: Any, text: str) -> None:
    await pilot.click("#prompt")
    await pilot.press(*text)
    await pilot.press("enter")


async def _settle(app: ConsoleApp, pilot: Any) -> None:
    await app.workers.wait_for_complete()
    await pilot.pause()


async def test_chat_streams_and_reports_cost(tmp_path: Path, helper_solution: Path) -> None:
    instance, approver, _ = await _open(
        tmp_path, helper_solution, [Message.assistant("Hello Ana, how can I help?")]
    )
    async with instance:
        agent = instance.agent()
        app = ConsoleApp(instance, agent, agent.new_session())
        approver.app = app
        async with app.run_test() as pilot:
            await _say(app, pilot, "hi")
            await _settle(app, pilot)
            assert "> hi" in app.transcript
            assert "Hello Ana, how can I help?" in app.transcript
            assert any(line.startswith("[end_turn, 1 turn(s)") for line in app.transcript)
            assert "You help Ana" in agent.system


@pytest.mark.parametrize(
    ("key", "approved", "asked_twice"),
    [("y", True, True), ("a", True, False), ("n", False, True)],
)
async def test_approvals(
    tmp_path: Path, helper_solution: Path, key: str, approved: bool, asked_twice: bool
) -> None:
    write = ("c1", "notes.write", {"key": "todo", "text": "call Luis"})
    again = ("c2", "notes.write", {"key": "todo", "text": "call Luis today"})
    script = [_calls(write), _calls(again), Message.assistant("Saved.")]
    instance, approver, _ = await _open(tmp_path, helper_solution, script)
    async with instance:
        agent = instance.agent()
        app = ConsoleApp(instance, agent, agent.new_session())
        approver.app = app
        async with app.run_test() as pilot:
            await _say(app, pilot, "save a note")
            for _ in range(2 if asked_twice else 1):
                while not isinstance(app.screen, ApprovalScreen):
                    await pilot.pause()
                assert "notes.write" in str(app.screen.request.tool)
                await pilot.press(key)
            await _settle(app, pilot)
            verdict = "approved" if approved else "denied"
            assert (
                f"{verdict}: notes.write" + ("" if asked_twice else " (session)") in app.transcript
            )
            status = "ok" if approved else "denied"
            assert f"<- notes.write: {status}" in " ".join(app.transcript)
            assert not isinstance(app.screen, ApprovalScreen)


async def test_commands_and_undo(tmp_path: Path, helper_solution: Path) -> None:
    edit = ("e1", "coding.edit", {"path": "app.py", "old": "hola", "new": "hello"})
    instance, approver, repo = await _open(
        tmp_path, helper_solution, [_calls(edit), Message.assistant("Edited.")]
    )
    async with instance:
        agent = instance.agent()
        app = ConsoleApp(instance, agent, agent.new_session())
        approver.app = app
        async with app.run_test() as pilot:
            await _say(app, pilot, "/tools")
            assert "coding.bash, coding.edit" in app.transcript[-1]
            await _say(app, pilot, "/undo")
            assert app.transcript[-1] == "nothing to undo"

            await _say(app, pilot, "fix greeting")
            while not isinstance(app.screen, ApprovalScreen):
                await pilot.pause()
            await pilot.press("y")
            await _settle(app, pilot)
            assert (repo / "app.py").read_text() == "print('hello')\n"

            await _say(app, pilot, "/undo")
            assert app.transcript[-1] == "undid coding.edit on app.py"
            assert (repo / "app.py").read_text() == "print('hola')\n"

            first = app.session.id
            await _say(app, pilot, "/new")
            assert app.session.id != first
            await _say(app, pilot, "/cost")
            assert app.transcript[-1].startswith("console cost so far: $")
            await _say(app, pilot, "/nope")
            assert "unknown command" in app.transcript[-1]
            await _say(app, pilot, "/quit")
        assert app.return_code in (0, None)
