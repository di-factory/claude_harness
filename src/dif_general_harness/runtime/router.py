"""The intent router short-circuit (ARCHITECTURE §3.5): cheap answers for trivial messages.

With ``policies.router`` (``short_circuit``: intents such as ``greeting``, ``thanks``,
``who_are_you``; ``model_role``: default ``router``), a contact's message is first classified
by the router role. When its intent is one of the short-circuit intents, the router's own
reply is the answer and the main agent does not run: no tools, no main-model cost.
Anything else, including an answer the router gets wrong (unreadable JSON, an unknown intent,
an empty reply), goes to the main agent: the router can only save work, never take a
decision.

Only conversations with a contact are routed; staff tasks, triggers and sub-agents always
run the agent.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ..core.events import Event, TurnEnded
from ..core.messages import Message
from ..core.session import Session
from ..providers.base import ModelRequest, ProviderMessage

if TYPE_CHECKING:
    from .instance import AgentRuntime

PERSONA_CHARS = 1500
ROUTER_PROMPT = """You triage messages for an assistant. The assistant's instructions begin:
---
{persona}
---
Classify the contact's message as one of: {intents}, or "other" (anything that needs the
assistant: questions, requests, complaints, or anything you are unsure about). For one of
{intents}, also write the reply the assistant would give: short, friendly, in the language
of the message, promising nothing. Answer with JSON only:
{{"intent": "<intent>", "reply": "<reply, or empty for other>"}}"""


def router_config(agent: AgentRuntime) -> tuple[list[str], str] | None:
    inst = agent.instance
    router: dict[str, Any] = inst.spec.policies.router or {}
    intents = [str(i) for i in router.get("short_circuit") or []]
    role = str(router.get("model_role") or "router")
    roles = inst.spec.models.roles if inst.spec.models else {}
    if not intents or role not in roles:
        return None
    return intents, role


async def short_circuit(
    agent: AgentRuntime, session: Session, safe_text: str
) -> list[Event] | None:
    """The events of a routed turn, or None when the main agent should answer."""
    config = router_config(agent)
    inst = agent.instance
    if config is None or session.contact_key is None or inst.provider is None:
        return None
    intents, role = config
    prompt = ROUTER_PROMPT.format(
        persona=agent.system[:PERSONA_CHARS], intents=", ".join(f'"{i}"' for i in intents)
    )
    request = ModelRequest(
        system=prompt, messages=[Message.user(safe_text)], tools=[], model_role=role
    )
    final: ProviderMessage | None = None
    try:
        async for event in inst.provider.stream(request):
            if isinstance(event, ProviderMessage):
                final = event
    except Exception:
        return None  # a router that is down never blocks the agent
    if final is None:
        return None
    priced = await inst.charge(agent.name, role, final)
    match = re.search(r"\{.*\}", final.message.text(), re.DOTALL)
    try:
        verdict = json.loads(match.group(0)) if match else {}
    except ValueError:
        return None
    intent, reply = str(verdict.get("intent") or ""), str(verdict.get("reply") or "").strip()
    if intent not in intents or not reply:
        return None
    await inst.audit.record(
        inst.scope, f"agent:{agent.name}", "router_answered", intent, {"session": session.id}
    )
    events: list[Event] = [
        session.add_message(Message.user(safe_text)),
        session.add_message(Message.assistant(reply)),
    ]
    events.append(
        session.stamp(
            TurnEnded(scope=session.scope, session_id=session.id, reason="end_turn", turns=1,
                      usage=priced)
        )
    )  # fmt: skip
    return events
