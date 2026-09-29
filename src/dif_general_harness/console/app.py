"""The TUI console (ARCHITECTURE §3.14): chat with one agent of a running instance.

Streams the agent's text, shows every tool call and its outcome, asks for approval in a
modal when the policy says ``ask``, and keeps a running cost. Commands:

  /help  /new  /session  /cost  /undo  /tools  /quit

The console is a surface, not a runtime: everything it shows comes from loop events, and
everything it persists goes through the instance's store.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Button, Footer, Header, Input, Label, Static

from ..core.events import ErrorEvent, TextDelta, ToolCallFinished, ToolCallStarted, TurnEnded
from ..core.messages import ToolStatus
from ..core.session import Session
from ..policy import ApprovalDecision, ApprovalRequest
from ..runtime import AgentRuntime, Instance

HELP = """Commands:
  /new      start a new session
  /session  show the session id (resume with --session)
  /cost     cost of this console run
  /undo     roll back the last file write or edit
  /tools    list the agent's tools
  /quit     leave"""


def _short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class ConsoleApprover:
    """Asks the person at the console. Bound to the app after it starts."""

    def __init__(self) -> None:
        self.app: ConsoleApp | None = None

    async def approve(self, request: ApprovalRequest) -> ApprovalDecision:
        if self.app is None:
            return ApprovalDecision(False, reason="the console is not running")
        return await self.app.ask(request)


class ApprovalScreen(ModalScreen[ApprovalDecision]):
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("y", "decide('once')", "Allow once"),
        Binding("a", "decide('session')", "Allow for session"),
        Binding("n", "decide('deny')", "Deny"),
        Binding("escape", "decide('deny')", "Deny", show=False),
    ]
    DEFAULT_CSS = """
    ApprovalScreen { align: center middle; }
    #box { width: 80%; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
    #buttons { height: auto; margin-top: 1; }
    Button { margin-right: 2; }
    """

    def __init__(self, request: ApprovalRequest) -> None:
        super().__init__()
        self.request = request

    def compose(self) -> ComposeResult:
        r = self.request
        why = f"rule {r.rule!r}" if r.rule else f"the policy for {r.effect} tools"
        with Vertical(id="box"):
            yield Label(f"Approve {r.tool}?  ({r.effect}; {why})")
            yield Static(_short(r.arguments, 1200), id="args")
            with Horizontal(id="buttons"):
                yield Button("Allow once [y]", id="once", variant="success")
                yield Button("Allow for session [a]", id="session", variant="primary")
                yield Button("Deny [n]", id="deny", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.action_decide(event.button.id or "deny")

    def action_decide(self, choice: str) -> None:
        if choice == "once":
            self.dismiss(ApprovalDecision(True))
        elif choice == "session":
            self.dismiss(ApprovalDecision(True, remember=True))
        else:
            self.dismiss(ApprovalDecision(False, reason="denied at the console"))


class ConsoleApp(App[None]):
    TITLE = "dif-general-harness"
    BINDINGS: ClassVar[list[BindingType]] = [Binding("ctrl+q", "quit", "Quit")]
    DEFAULT_CSS = """
    #log { height: 1fr; padding: 0 1; }
    .user { color: $accent; margin-top: 1; }
    .assistant { margin-top: 1; }
    .tool { color: $text-muted; }
    .denied, .error { color: $error; }
    .info { color: $text-muted; }
    #prompt { dock: bottom; }
    """

    def __init__(self, instance: Instance, agent: AgentRuntime, session: Session) -> None:
        super().__init__()
        self.instance = instance
        self.agent = agent
        self.session = session
        self.cost_usd = 0.0
        self.transcript: list[str] = []  # plain-text copy of what is shown, for tests and logs
        self._reply: Static | None = None
        self._reply_text = ""
        self._reply_index = 0
        self.sub_title = f"{instance.spec.solution.id} / {agent.name}"

    def compose(self) -> ComposeResult:
        yield Header()
        yield VerticalScroll(id="log")
        yield Input(placeholder="Message, or /help", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        for issue in self.instance.issues:
            self.show(str(issue), "info")
        if self.agent.missing_tools:
            self.show(f"tools not available: {', '.join(self.agent.missing_tools)}", "info")
        self.show(f"session {self.session.id}. Type /help for commands.", "info")
        self.query_one("#prompt", Input).focus()

    # --- output --------------------------------------------------------------------

    def show(self, text: str, kind: str) -> Static:
        self.transcript.append(text)
        widget = Static(text, classes=kind, markup=False)
        log = self.query_one("#log", VerticalScroll)
        log.mount(widget)
        log.scroll_end(animate=False)
        return widget

    def _stream(self, text: str) -> None:
        if self._reply is None:
            self._reply_text = ""
            self._reply = self.show("", "assistant")
            self._reply_index = len(self.transcript) - 1
        self._reply_text += text
        self._reply.update(self._reply_text)
        self.transcript[self._reply_index] = self._reply_text
        self.query_one("#log", VerticalScroll).scroll_end(animate=False)

    def _end_reply(self) -> None:
        self._reply = None

    # --- input ---------------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if not text:
            return
        if text.startswith("/"):
            await self.command(text)
            return
        self.show(f"> {text}", "user")
        event.input.disabled = True
        self.run_worker(self.turn(text), exclusive=True, group="turn")

    async def turn(self, text: str) -> None:
        try:
            async for event in self.agent.send(self.session, text):
                if isinstance(event, TextDelta):
                    self._stream(event.text)
                    continue
                self._end_reply()
                if isinstance(event, ToolCallStarted):
                    self.show(f"-> {event.name} {_short(event.input)}", "tool")
                elif isinstance(event, ToolCallFinished):
                    kind = {ToolStatus.OK: "tool", ToolStatus.DENIED: "denied"}.get(
                        event.status, "error"
                    )
                    self.show(f"<- {event.name}: {event.status} ({event.duration_ms} ms)", kind)
                elif isinstance(event, ErrorEvent):
                    self.show(f"error: {event.message}", "error")
                elif isinstance(event, TurnEnded):
                    self.cost_usd += event.usage.cost_usd
                    self.show(
                        f"[{event.reason}, {event.turns} turn(s), ${event.usage.cost_usd:.4f}]",
                        "info",
                    )
        finally:
            self._end_reply()
            prompt = self.query_one("#prompt", Input)
            prompt.disabled = False
            prompt.focus()

    async def ask(self, request: ApprovalRequest) -> ApprovalDecision:
        self._end_reply()
        decision: ApprovalDecision = await self.push_screen(
            ApprovalScreen(request), wait_for_dismiss=True
        )
        verdict = "approved" if decision.approved else "denied"
        self.show(
            f"{verdict}: {request.tool}" + (" (session)" if decision.remember else ""), "info"
        )
        return decision

    async def command(self, text: str) -> None:
        name = text.split()[0].lower()
        if name == "/help":
            self.show(HELP, "info")
        elif name == "/quit":
            self.exit()
        elif name == "/new":
            self.session = await self.agent.new_session()
            self.show(f"new session {self.session.id}", "info")
        elif name == "/session":
            self.show(f"session {self.session.id}", "info")
        elif name == "/cost":
            self.show(f"console cost so far: ${self.cost_usd:.4f}", "info")
        elif name == "/tools":
            self.show(", ".join(self.agent.tools.names()) or "no tools", "info")
        elif name == "/undo":
            ws = self.instance.workspace
            cp = ws.undo() if ws else None
            if cp is None:
                self.show("nothing to undo", "info")
            else:
                self.show(f"undid {cp.tool} on {ws.rel(cp.path) if ws else cp.path}", "info")
        else:
            self.show(f"unknown command {name}; /help lists them", "error")
