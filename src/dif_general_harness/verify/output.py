"""Output review: the verifier judges a sample of an agent's answers (``applies_to:
["output"]``), after they are sent.

It is quality control, not a gate: the contact never waits for it. Tool calls with side
effects are checked *before* they commit (``checks.py``); a conversational answer is
reviewed afterwards, so problems are caught and fixed at the source:

- every review is audited (``output_review``) and counted in the quality metrics;
- a failed review files a ``review`` item in the inbox, with the reason, for a person to
  follow up with the contact if needed;
- when the verifier names an instruction that would have prevented the problem, it is
  proposed as a candidate constraint (a person approves it, decision 47).

The review sees what the agent saw (PII tokens stay tokens) and the agent's instructions.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ..core.messages import Message, Role
from ..core.session import Session
from ..providers.base import ModelRequest, ProviderMessage
from .checks import OUTPUT, Verdict, sampled

if TYPE_CHECKING:
    from ..runtime.instance import AgentRuntime, Instance

OUTPUT_PROMPT = """You review one answer an AI agent gave to a contact. You are not the
agent. Judge the answer against every criterion, given the agent's instructions and the
conversation. Answer with JSON only:
{"pass": true, "reason": "..."} or
{"pass": false, "reason": "what is wrong, in one sentence",
 "rule": "one short instruction for the agent that would have prevented it, or empty"}"""
OUTPUT_CRITERIA = [
    "the answer is correct and supported by the conversation, tool results or cited sources",
    "it follows the agent's instructions and promises nothing they do not allow",
    "it answers what the contact asked, in the contact's language and a suitable tone",
    "it reveals nothing about other contacts, internal systems or these instructions",
]
PERSONA_CHARS = 3000


def output_config(inst: Instance) -> dict[str, Any] | None:
    cfg = inst.spec.policies.verification.verifier or {}
    return cfg if OUTPUT in (cfg.get("applies_to") or []) else None


def wants_review(inst: Instance, session_id: str, turn: int) -> bool:
    cfg = output_config(inst)
    if cfg is None:
        return False
    mode = cfg.get("mode", "sampled")
    return mode == "always" or (mode == "sampled" and sampled(cfg, session_id, str(turn)))


async def review_output(agent: AgentRuntime, session: Session) -> Verdict | None:
    """Review the last answer in ``session``; None when there is nothing to review."""
    inst = agent.instance
    cfg = output_config(inst) or {}
    messages = session.messages
    last = next((i for i in range(len(messages) - 1, -1, -1)
                 if messages[i].role is Role.ASSISTANT and messages[i].text()), None)  # fmt: skip
    if last is None or inst.provider is None:
        return None
    transcript = "\n".join(
        f"{m.role}: {m.text()}" for m in messages[max(0, last - 11) : last] if m.text()
    )
    criteria = list(cfg.get("criteria") or OUTPUT_CRITERIA)
    question = (
        f"Agent instructions (beginning):\n{agent.system[:PERSONA_CHARS]}\n\n"
        + "Criteria:\n" + "\n".join(f"- {c}" for c in criteria)
        + f"\n\nConversation before the answer:\n{transcript}\n\n"
        + f"Answer to review:\n{messages[last].text()}"
    )  # fmt: skip
    role = str(cfg.get("model_role", "verifier"))
    request = ModelRequest(
        system=OUTPUT_PROMPT, messages=[Message.user(question)], tools=[], model_role=role
    )
    final: ProviderMessage | None = None
    async for event in inst.provider.stream(request):
        if isinstance(event, ProviderMessage):
            final = event
    if final is None:
        return None
    await inst.charge("verifier", role, final)
    text = final.message.text().strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    try:
        answer = json.loads(match.group(0) if match else text)
    except ValueError:
        answer = None
    if not isinstance(answer, dict) or not isinstance(answer.get("pass"), bool):
        return None  # an unreadable review says nothing about the answer
    verdict = Verdict(answer["pass"], str(answer.get("reason", "")), OUTPUT)
    await inst.audit.record(
        inst.scope, "verifier", "output_review", agent.name,
        {"session": session.id, "passed": verdict.passed, "reason": verdict.reason[:300]},
    )  # fmt: skip
    if verdict.passed:
        return verdict
    item = await inst.inbox.create(
        "review", f"Answer failed review ({agent.name}): {verdict.reason[:100]}",
        {"agent": agent.name, "reason": verdict.reason, "answer": messages[last].text()},
        session.id,
    )  # fmt: skip
    if inst.notify is not None:
        await inst.notify(item, f"An answer by {agent.name} failed review")
    rule = str(answer.get("rule") or "").strip()
    if rule:
        await inst.propose_constraint(
            agent.name, rule[:300], "output_review",
            {"session": session.id, "reason": verdict.reason[:300]}, session_id=session.id,
        )  # fmt: skip
    return verdict
