"""Replay real conversations on a rebuilt client before it goes online.

A fine-tuning round changes answers, settings or the pack; whether that helped is only
known when customers ask again. ``replay`` asks now: the latest real conversations (from the
running instance's admin API) are sent again, turn by turn, to the rebuilt client in a
throwaway instance, exactly as an eval case (channels record instead of sending, approvals
are never granted, nothing reaches a customer, and every tool that writes or acts on an outside
system answers "not run" instead of running: read-only tools such as knowledge search or a
calendar lookup still run, so the replies stay comparable). A judge model (the
pack's ``verifier`` role) compares each new reply with the one the customer got:

- ``same``: the same facts and intent, in other words;
- ``better``: more correct, complete or helpful for that customer message;
- ``worse``: wrong, missing what the customer needed, or unsafe;
- ``changed``: different, and neither clearly better nor worse.

The report (Markdown) shows every turn side by side; ``worse`` replies are listed first.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from ..core.messages import Message
from ..core.session import SUMMARY_HEADER
from ..providers.base import ModelRequest, ProviderMessage
from ..runtime import Instance
from ..tools.registry import Effect
from .evals import CaseResult, OpenInstance, _Case, _fixture

NOT_RUN = {"replay": "not run: this is a replayed conversation; assume the action succeeded"}

Verdict = Literal["same", "better", "worse", "changed", "unjudged"]
JUDGE_SYSTEM = """You compare two replies an assistant gave to the same customer message: the
reply the customer actually got (BEFORE) and the reply of a new version (AFTER). Judge only
for that customer: is AFTER correct, complete and helpful, and does it avoid inventing facts?
Answer with JSON only: {"verdict": "same" | "better" | "worse" | "changed", "why": "<one
short sentence>"}. "same": the same facts and intent in other words. "changed": different, and
neither clearly better nor worse. Never reward length for its own sake."""


@dataclass
class Turn:
    customer: str
    before: str
    after: str = ""
    verdict: Verdict = "unjudged"
    why: str = ""


@dataclass
class Replayed:
    session: str
    channel: str | None
    turns: list[Turn] = field(default_factory=list)
    error: str = ""


def conversations(sessions: list[dict[str, Any]]) -> list[Replayed]:
    """Admin ``/admin/sessions`` rows as replayable conversations: each customer message
    with what the assistant answered it (automatic notes and summaries left out)."""
    out = []
    for row in sessions:
        convo = Replayed(str(row["id"]), row.get("channel"))
        for message in row.get("messages") or []:
            text = str(message.get("text") or "").strip()
            if not text or text.startswith(("[Automatic check:", SUMMARY_HEADER)):
                continue
            if message.get("role") == "user":
                convo.turns.append(Turn(customer=text, before=""))
            elif convo.turns:
                last = convo.turns[-1]
                last.before = f"{last.before}\n\n{text}".strip()
        if convo.turns:
            out.append(convo)
    return out


async def replay(
    open_instance: OpenInstance, convos: list[Replayed], work: Path, *, judge: bool = True
) -> list[Replayed]:
    """Each conversation again, in its own throwaway instance of the rebuilt client."""
    for index, convo in enumerate(convos, 1):
        inst = await open_instance(work / f"replay-{index}")
        async with inst:
            for name in inst.tools.names():  # nothing a replay does may reach the world
                tool = inst.tools.get(name)
                if tool is not None and tool.effect is not Effect.READ:
                    inst.tools.replace(_fixture(name, NOT_RUN, tool))
            setup: dict[str, Any] = {}
            if convo.channel and convo.channel in inst.spec.channels:
                setup["channel"] = convo.channel
            case = {"id": convo.session, "setup": setup, "turns": []}
            runner = _Case(inst, case, CaseResult("replay", convo.session, "passed"), index)
            try:
                await runner.open()
                for turn in convo.turns:
                    before = len(runner.outbox)
                    await runner.say(turn.customer)
                    turn.after = "\n\n".join(s.text for s in runner.outbox[before:]).strip()
                    if judge:
                        turn.verdict, turn.why = await _judge(inst, turn)
            except Exception as exc:  # a crashed replay is reported, never counted as same
                convo.error = f"{type(exc).__name__}: {exc}"
    return convos


async def _judge(inst: Instance, turn: Turn) -> tuple[Verdict, str]:
    roles = inst.spec.models.roles if inst.spec.models else {}
    if "verifier" not in roles or inst.provider is None:
        return "unjudged", "no verifier model role to judge with"
    if turn.before.strip() == turn.after.strip():
        return "same", "identical reply"
    request = ModelRequest(
        system=JUDGE_SYSTEM,
        messages=[Message.user(f"Customer: {turn.customer}\n\nBEFORE:\n{turn.before or '(no'
                               ' reply)'}\n\nAFTER:\n{turn.after or '(no reply)'}")],
        tools=[],
        model_role="verifier",
    )  # fmt: skip
    final: ProviderMessage | None = None
    async for event in inst.provider.stream(request):
        if isinstance(event, ProviderMessage):
            final = event
    if final is not None:
        await inst.charge("replay-judge", "verifier", final)
    text = final.message.text() if final else ""
    found = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        data = json.loads(found.group(0)) if found else {}
    except ValueError:
        data = {}
    verdict = data.get("verdict") if isinstance(data, dict) else None
    if verdict not in ("same", "better", "worse", "changed"):
        return "unjudged", "the judge's answer could not be read"
    return verdict, str(data.get("why") or "")[:300]


def counts(convos: list[Replayed]) -> dict[str, int]:
    out = {"same": 0, "better": 0, "worse": 0, "changed": 0, "unjudged": 0}
    for convo in convos:
        for turn in convo.turns:
            out[turn.verdict] += 1
    return out


def summary(convos: list[Replayed]) -> str:
    c = counts(convos)
    errors = sum(1 for convo in convos if convo.error)
    line = (f"{sum(c.values())} customer message(s) in {len(convos)} conversation(s):"
            f" {c['worse']} worse, {c['better']} better, {c['changed']} changed,"
            f" {c['same']} same")  # fmt: skip
    if c["unjudged"]:
        line += f", {c['unjudged']} not judged"
    if errors:
        line += f"; {errors} conversation(s) could not be replayed"
    return line


def report(convos: list[Replayed], title: str) -> str:
    """Markdown: the summary, then every turn side by side, worse ones first."""
    order = {"worse": 0, "changed": 1, "better": 2, "unjudged": 3, "same": 4}
    out = [f"# Replay: {title}", "", summary(convos), ""]
    ranked = sorted(convos, key=lambda c: min((order[t.verdict] for t in c.turns), default=9))
    for convo in ranked:
        out += [f"## Conversation {convo.session}" + (f" ({convo.channel})" if convo.channel
                                                      else ""), ""]  # fmt: skip
        if convo.error:
            out += [f"Could not be replayed: {convo.error}", ""]
        for turn in convo.turns:
            out += [f"**Customer:** {turn.customer}", "",
                    f"**{turn.verdict.upper()}**" + (f": {turn.why}" if turn.why else ""), "",
                    "Before:", "", _quote(turn.before), "", "After:", "", _quote(turn.after),
                    ""]  # fmt: skip
    return "\n".join(out).rstrip() + "\n"


def _quote(text: str) -> str:
    return "\n".join(f"> {line}" for line in (text or "(no reply)").splitlines())
