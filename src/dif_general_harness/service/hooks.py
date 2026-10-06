"""Hooks: the client's own systems told what happened, as signed JSON POSTs.

``hooks`` in the spec name a URL, the events it wants and the secret it is signed with::

    "hooks": {"crm": {"url": "https://crm.example.com/dif", "events": ["escalation",
              "tool_call"], "secret": {"$secret": "crm_hook_key"}, "tools": ["calendar.*"]}}

Events (from conversations through the channels): ``turn_end`` (every reply), ``tool_call``
(each call in a turn, its tool, effect and outcome; ``tools`` narrows them), ``escalation``
(a conversation handed to a person) and ``handoff`` (to a teammate agent). Payloads carry
ids, names and outcomes, never message texts, tool inputs or contact details, unless the
hook sets ``texts: true`` (then the reply as the customer got it, and the escalation reason).
The contact is a stable reference (``contact_ref``, a hash), so a client can count repeat
contacts without receiving who they are.

Each delivery is a durable job (retried with backoff, never blocking a reply). It carries
``X-Dif-Event``, ``X-Dif-Timestamp`` and ``X-Dif-Signature: sha256=<hex>``, the HMAC-SHA256
of ``<timestamp>.<body>`` with the hook's secret; a receiver recomputes it and rejects
anything older than a few minutes.
"""

from __future__ import annotations

import fnmatch
import hashlib
import hmac
import json
import time
from typing import Any

EVENTS = ("turn_end", "tool_call", "escalation", "handoff")


def sign(secret: str, timestamp: str, body: bytes) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def verify(secret: str, timestamp: str, body: bytes, signature: str, *, max_age_s: int = 300,
           now: float | None = None) -> bool:  # fmt: skip
    """What a receiver does (and what the tests use)."""
    try:
        age = abs((now if now is not None else time.time()) - float(timestamp))
    except ValueError:
        return False
    return age <= max_age_s and hmac.compare_digest(sign(secret, timestamp, body), signature)


def contact_ref(tenant: str, contact: str | None) -> str | None:
    if not contact:
        return None
    return hashlib.sha256(f"{tenant}\0{contact}".encode()).hexdigest()[:16]


def wants(hook: Any, event: str, data: dict[str, Any]) -> bool:
    if event not in hook.events:
        return False
    if event == "tool_call" and hook.tools:
        return any(fnmatch.fnmatchcase(str(data.get("tool")), p) for p in hook.tools)
    return True


def body(event: str, data: dict[str, Any], *, tenant: str, instance: str, at: float) -> bytes:
    payload = {"event": event, "tenant": tenant, "instance": instance, "at": int(at), **data}
    return json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
