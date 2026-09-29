"""PII tokenization (ARCHITECTURE §3.18): personal data never reaches the model.

Inbound text and tool results are tokenized: each detected value becomes a stable token
such as ``<PHONE_3f9a1c2b>`` (the same value always gets the same token within an
instance, so the model can still reason about "the same phone"). The vault maps tokens back
to values, per tenant and instance, in the client's own database.

Tokens are resolved only where the spec allows (``governance.pii``):
- ``reveal_in_output``: classes shown in replies to the contact; other tokens in a reply
  are masked (``[phone]``);
- ``reveal_to_tools``: classes a tool receives in clear (the RFC must reach the ERP).

Detectors: email, phone (Mexican formats), CURP, RFC, account (CLABE and card numbers with a
Luhn check) by pattern, and name from the names the channel knows (the contact's profile)
plus self-introductions ("me llamo Ana", "my name is Ana"). Classes without a detector
(``health``, ``address``) are reported by ``undetectable`` so the runtime can warn: a
pattern cannot find them and pretending otherwise would be unsafe.
"""

from __future__ import annotations

import fnmatch
import re
import secrets
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from ..core.scope import Scope
from ..store.db import Database

_TOKEN = re.compile(r"<([A-Z]+)_([0-9a-f]{8})>")

_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}"
)
_PHONE = re.compile(
    r"(?<![\w+])(?:\+?52[\s.-]?)?(?:1[\s.-]?)?(?:\(?\d{2,3}\)?[\s.-]?)\d{3,4}[\s.-]?\d{4}(?!\w)"
)
_CURP = re.compile(r"\b[A-Z][AEIOUX][A-Z]{2}\d{6}[HM][A-Z]{5}[0-9A-Z]\d\b", re.IGNORECASE)
_RFC = re.compile(r"\b[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}\b", re.IGNORECASE)
_DIGITS = re.compile(r"(?<!\d)(?:\d[ -]?){13,18}\d(?!\d)")
_INTRO = re.compile(
    r"\b(?i:me llamo|mi nombre es|soy|my name is|i am|i'm)\s+"
    r"([A-ZÁÉÍÓÚÑ][a-záéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][a-záéíóúñ]+){0,2})"
)

DETECTABLE = {"email", "phone", "curp", "rfc", "account", "name"}


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def normalize(kind: str, value: str) -> str:
    """One canonical form per value, so '55 1234 5678' and '+52 5512345678' share a token."""
    if kind == "phone":
        return re.sub(r"\D", "", value)[-10:]
    if kind == "account":
        return re.sub(r"\D", "", value)
    if kind == "email":
        return value.lower()
    if kind in {"curp", "rfc"}:
        return value.upper()
    return " ".join(value.split())


def find(text: str, classes: set[str], names: Iterable[str] = ()) -> list[tuple[int, int, str]]:
    """Spans ``(start, end, kind)`` of PII in ``text``, non-overlapping, left to right."""
    spans: list[tuple[int, int, str]] = []

    def add(pattern: re.Pattern[str], kind: str, group: int = 0) -> None:
        for m in pattern.finditer(text):
            spans.append((m.start(group), m.end(group), kind))

    if "email" in classes:
        add(_EMAIL, "email")
    if "curp" in classes:
        add(_CURP, "curp")
    if "rfc" in classes:
        add(_RFC, "rfc")
    if "account" in classes:
        for m in _DIGITS.finditer(text):
            digits = re.sub(r"\D", "", m.group(0))
            if len(digits) == 18 or (13 <= len(digits) <= 19 and _luhn(digits)):
                spans.append((m.start(), m.end(), "account"))
    if "phone" in classes:
        for m in _PHONE.finditer(text):
            if len(re.sub(r"\D", "", m.group(0))) >= 10:
                spans.append((m.start(), m.end(), "phone"))
    if "name" in classes:
        add(_INTRO, "name", 1)
        for name in sorted(
            {n.strip() for n in names if n and len(n.strip()) >= 2}, key=len, reverse=True
        ):
            for m in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE):
                spans.append((m.start(), m.end(), "name"))
    # earlier and longer spans win; tokens already in the text are never re-tokenized
    taken = [(m.start(), m.end()) for m in _TOKEN.finditer(text)]
    out: list[tuple[int, int, str]] = []
    for start, end, kind in sorted(spans, key=lambda s: (s[0], -(s[1] - s[0]))):
        if any(start < e and end > s for s, e in taken):
            continue
        out.append((start, end, kind))
        taken.append((start, end))
    return sorted(out)


class TokenVault:
    """Token <-> value, per tenant and instance, in the instance's database."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self._cache: dict[tuple[str, str, str], str] = {}

    async def token_for(self, scope: Scope, kind: str, value: str) -> str:
        norm = normalize(kind, value)
        key = (f"{scope.tenant_id}/{scope.instance_id}", kind, norm)
        if key in self._cache:
            return self._cache[key]
        where = (scope.tenant_id, scope.instance_id, kind, norm)
        row = await self.db.fetchone(
            "SELECT token FROM pii_tokens WHERE tenant_id = ? AND instance_id = ? AND kind = ?"
            " AND value = ?",
            where,
        )
        if row is None:
            token = f"<{kind.upper()}_{secrets.token_hex(4)}>"
            await self.db.execute(
                "INSERT INTO pii_tokens (tenant_id, instance_id, token, kind, value, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (scope.tenant_id, scope.instance_id, token, kind, norm, time.time()),
            )
            row = await self.db.fetchone(  # a concurrent writer may have won the race
                "SELECT token FROM pii_tokens WHERE tenant_id = ? AND instance_id = ?"
                " AND kind = ? AND value = ?",
                where,
            )
            assert row is not None
        self._cache[key] = str(row["token"])
        return self._cache[key]

    async def names(self, scope: Scope) -> set[str]:
        """Every name this instance has tokenized, so later text (a tool result, another
        message) is checked for them too."""
        rows = await self.db.fetchall(
            "SELECT value FROM pii_tokens WHERE tenant_id = ? AND instance_id = ?"
            " AND kind = 'name'",
            (scope.tenant_id, scope.instance_id),
        )
        return {str(r["value"]) for r in rows}

    async def value_of(self, scope: Scope, token: str) -> tuple[str, str] | None:
        row = await self.db.fetchone(
            "SELECT kind, value FROM pii_tokens WHERE tenant_id = ? AND instance_id = ?"
            " AND token = ?",
            (scope.tenant_id, scope.instance_id, token),
        )
        return (str(row["kind"]), str(row["value"])) if row else None


@dataclass
class PiiPolicy:
    classes: set[str]
    tokenize: bool = True
    reveal_in_output: set[str] = field(default_factory=set)
    reveal_to_tools: dict[str, set[str]] = field(default_factory=dict)

    @classmethod
    def from_spec(cls, pii: Any) -> PiiPolicy:
        return cls(
            classes=set(pii.classes),
            tokenize=bool(pii.tokenize),
            reveal_in_output=set(pii.reveal_in_output),
            reveal_to_tools={k: set(v) for k, v in pii.reveal_to_tools.items()},
        )

    @property
    def undetectable(self) -> set[str]:
        return self.classes - DETECTABLE

    def for_tool(self, tool: str) -> set[str]:
        out: set[str] = set()
        for pattern, classes in self.reveal_to_tools.items():
            if fnmatch.fnmatchcase(tool, pattern):
                out |= classes
        return out


class Tokenizer:
    def __init__(self, policy: PiiPolicy, vault: TokenVault, scope: Scope) -> None:
        self.policy = policy
        self.vault = vault
        self.scope = scope
        self._names: set[str] | None = None  # loaded from the vault on first use

    @property
    def active(self) -> bool:
        return self.policy.tokenize and bool(self.policy.classes & DETECTABLE)

    async def tokenize(self, text: str, names: Iterable[str] = ()) -> str:
        if not self.active:
            return text
        if self._names is None:
            self._names = (
                await self.vault.names(self.scope) if "name" in self.policy.classes else set()
            )
        out, last = [], 0
        for start, end, kind in find(text, self.policy.classes, [*self._names, *names]):
            value = text[start:end]
            out += [text[last:start], await self.vault.token_for(self.scope, kind, value)]
            if kind == "name":
                self._names.add(normalize(kind, value))
            last = end
        return "".join(out) + text[last:]

    async def detokenize(self, text: str, reveal: set[str], *, mask: bool = True) -> str:
        """Tokens of ``reveal`` classes become values; others become ``[kind]`` (or stay)."""
        out, last = [], 0
        for m in _TOKEN.finditer(text):
            kind = m.group(1).lower()
            resolved = await self.vault.value_of(self.scope, m.group(0)) if kind in reveal else None
            replacement = resolved[1] if resolved else (f"[{kind}]" if mask else m.group(0))
            out += [text[last : m.start()], replacement]
            last = m.end()
        return "".join(out) + text[last:]

    async def tokenize_obj(self, obj: Any, names: Iterable[str] = ()) -> Any:
        if isinstance(obj, str):
            return await self.tokenize(obj, names)
        if isinstance(obj, dict):
            return {k: await self.tokenize_obj(v, names) for k, v in obj.items()}
        if isinstance(obj, list):
            return [await self.tokenize_obj(v, names) for v in obj]
        return obj

    async def detokenize_obj(self, obj: Any, reveal: set[str]) -> Any:
        if isinstance(obj, str):
            return await self.detokenize(obj, reveal, mask=False)
        if isinstance(obj, dict):
            return {k: await self.detokenize_obj(v, reveal) for k, v in obj.items()}
        if isinstance(obj, list):
            return [await self.detokenize_obj(v, reveal) for v in obj]
        return obj
