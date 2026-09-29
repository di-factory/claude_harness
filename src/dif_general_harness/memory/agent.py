"""Memory in an agent's run: what it remembers at the start of a turn, the ``memory.*`` tools,
and what gets written back.

- **Retrieval:** active facts, approved skills and recent episodes of the agent's memory
  scope go into a "what you remember" block of the system prompt, within ``MEMORY_BUDGET``
  characters (newest first). Pinned constraints are never cut; memory is.
- **Tools:** ``memory.search(query)``, ``memory.write(key, value)`` (a fact; a changed value
  supersedes the old one and a person is told), ``memory.propose_skill(name, steps)`` (counted;
  after ``skill_promotion.min_successes`` it goes to a person for approval).
- **Episodes:** each turn updates its conversation's episode (what was asked, what was
  answered), expiring after ``episodic_ttl``.
- **Extraction:** with a ``memory_extraction`` model role, the service distils facts from a
  conversation in the background (``extract_facts``).
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from ..core.messages import Message
from ..core.session import Session
from ..providers.base import ModelRequest, ProviderMessage
from ..spec.loader import duration_days
from ..tools.registry import Effect, Tool, schema_check
from .store import MemoryScope, MemoryStore

if TYPE_CHECKING:
    from ..runtime.instance import AgentRuntime, Instance

MEMORY_BUDGET = 3000  # characters of remembered context per turn
EXTRACT_PROMPT = """You extract durable facts about the contact from a conversation, for a
memory that later conversations will read. Only facts the contact stated or confirmed, that
will still matter next time (preferences, constraints, relationships, standing requests).
No small talk, nothing temporary, nothing you infer. Keep PII tokens like <PHONE_1a2b3c4d>
exactly as written. Answer with JSON only: {"facts": [{"key": "short stable name", "value":
"the fact"}]}; an empty list when there is nothing durable."""


def memory_config(instance: Instance, agent_name: str) -> tuple[list[str], str] | None:
    """(layers, scope kind) for an agent, or None when it has no memory."""
    spec = instance.spec
    base = spec.memory
    own = spec.agents[agent_name].memory or {}
    layers = list(own.get("layers") or (base.layers if base else []))
    if not layers and not own:
        return None
    kind = str(own.get("scope") or (base.scope if base else "instance"))
    return (layers or ["episodic", "semantic"], kind)


def scope_for(kind: str, agent_name: str, session: Session) -> MemoryScope | None:
    if kind == "contact":
        return MemoryScope("contact", session.contact_key) if session.contact_key else None
    if kind == "agent":
        return MemoryScope("agent", agent_name)
    return MemoryScope("instance", "")


async def memory_block(instance: Instance, agent_name: str, session: Session) -> str:
    cfg = memory_config(instance, agent_name)
    if cfg is None:
        return ""
    layers, kind = cfg
    ms = scope_for(kind, agent_name, session)
    if ms is None:
        return ""
    store: MemoryStore = instance.memory
    lines: list[str] = []
    if "semantic" in layers:
        lines += [f"- {m.key}: {m.content}" for m in await store.active(ms, "semantic")]
    if "procedural" in layers:
        lines += [f"- skill '{m.key}': {m.content}" for m in await store.active(ms, "procedural")]
    if "episodic" in layers:
        episodes = [m for m in await store.active(ms, "episodic") if m.key != session.id][:3]
        lines += [f"- earlier conversation: {m.content}" for m in episodes]
    block, used = [], 0
    for line in lines:
        if used + len(line) > MEMORY_BUDGET:
            break
        block.append(line)
        used += len(line)
    if not block:
        return ""
    return "\n\n## What you remember\n" + "\n".join(block)


def memory_tools(instance: Instance, agent: AgentRuntime) -> list[Tool]:
    cfg = memory_config(instance, agent.name)
    if cfg is None:
        return []
    from ..runtime.context import current_session  # runtime imports this module

    layers, kind = cfg
    store = instance.memory

    def scope() -> MemoryScope:
        session = current_session.get()
        ms = scope_for(kind, agent.name, session) if session else None
        if ms is None:
            raise RuntimeError("there is no contact to remember things about")
        return ms

    async def search(query: str) -> list[dict[str, Any]]:
        found = await store.search(scope(), query)
        return [
            {"key": m.key, "content": m.content, "layer": m.layer, "score": round(s, 2)}
            for m, s in found
        ]

    async def write(key: str, value: str) -> str:
        session = current_session.get()
        mem_id, previous = await store.remember(
            scope(), key, value, session.id if session else None
        )
        if previous is not None:
            await instance.flag_contradiction(previous, value, mem_id)
            return (
                f"remembered; this replaces the earlier value ({previous.content!r})"
                " and a person was told"
            )
        return "remembered"

    async def propose_skill(name: str, steps: str) -> str:
        session = current_session.get()
        skill = await store.propose_skill(scope(), name, steps, session.id if session else None)
        return await instance.consider_skill(skill)

    def make(name: str, fn: Any, props: dict[str, Any], effect: Effect, doc: str) -> Tool:
        schema = {"type": "object", "properties": props, "required": list(props)}
        return Tool(
            name=name,
            description=doc,
            input_schema=schema,
            handler=fn,
            effect=effect,
            check_input=schema_check(schema),
            source="memory",
        )

    text = {"type": "string"}
    tools = [
        make(
            "memory.search",
            search,
            {"query": text},
            Effect.READ,
            "Search what you remember about this contact (facts, skills, past conversations).",
        )
    ]
    if "semantic" in layers:
        tools.append(
            make(
                "memory.write",
                write,
                {"key": text, "value": text},
                Effect.WRITE,
                "Remember a durable fact the contact stated (a preference, a constraint).",
            )
        )
    if "procedural" in layers:
        tools.append(
            make(
                "memory.propose_skill",
                propose_skill,
                {"name": text, "steps": text},
                Effect.WRITE,
                "Propose a reusable way of doing a task that worked; a person approves it.",
            )
        )
    return tools


async def record_episode(instance: Instance, agent_name: str, session: Session) -> None:
    cfg = memory_config(instance, agent_name)
    if cfg is None or "episodic" not in cfg[0]:
        return
    ms = scope_for(cfg[1], agent_name, session)
    if ms is None:
        return
    exchanges = [f"{m.role}: {m.text()}" for m in session.messages if m.text()][-8:]
    if not exchanges:
        return
    base = instance.spec.memory
    ttl_text = (instance.spec.agents[agent_name].memory or {}).get("episodic_ttl") or (
        base.episodic_ttl if base else None
    )
    retention = instance.spec.governance.retention.get("episodic")
    ttls = [duration_days(t) * 86400 for t in (ttl_text, retention) if t]
    await instance.memory.record_episode(
        ms, session.id, " | ".join(exchanges)[:2000], min(ttls) if ttls else None
    )


async def extract_facts(instance: Instance, agent_name: str, session: Session) -> int:
    """Distil durable facts with the ``memory_extraction`` role; returns how many were stored."""
    cfg = memory_config(instance, agent_name)
    roles = instance.spec.models.roles if instance.spec.models else {}
    if cfg is None or "semantic" not in cfg[0] or "memory_extraction" not in roles:
        return 0
    ms = scope_for(cfg[1], agent_name, session)
    if ms is None or instance.provider is None:
        return 0
    transcript = "\n".join(f"{m.role}: {m.text()}" for m in session.messages[-20:] if m.text())
    request = ModelRequest(
        system=EXTRACT_PROMPT,
        messages=[Message.user(transcript)],
        tools=[],
        model_role="memory_extraction",
    )
    final: ProviderMessage | None = None
    async for event in instance.provider.stream(request):
        if isinstance(event, ProviderMessage):
            final = event
    if final is None:
        return 0
    match = re.search(r"\{.*\}", final.message.text(), re.DOTALL)
    try:
        facts = json.loads(match.group(0))["facts"] if match else []
    except (ValueError, KeyError, TypeError):
        return 0
    stored = 0
    for fact in facts if isinstance(facts, list) else []:
        if (
            isinstance(fact, dict)
            and isinstance(fact.get("key"), str)
            and isinstance(fact.get("value"), str)
        ):
            value = instance.redactor.redact(fact["value"])
            mem_id, previous = await instance.memory.remember(ms, fact["key"], value, session.id)
            if previous is not None:
                await instance.flag_contradiction(previous, value, mem_id)
            stored += 1
    return stored
