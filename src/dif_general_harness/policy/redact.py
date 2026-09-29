"""Secret redaction for logs, traces and memory (ARCHITECTURE §3.12, decision 19).

Two layers: exact values of every resolved secret (registered at runtime) and patterns
for well-known credential formats. A high-entropy fallback catches unknown tokens but
only for strings mixing upper case, lower case and digits, so ids and hashes survive.
Patterns never span a double quote, so redacting serialized JSON keeps it valid.
``redact_obj`` redacts every string inside a JSON-like structure (keys included).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

MASK = "[REDACTED]"
_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"),
    re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),  # JWT
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[^\"]*?-----END [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{16,}"),
]
_CANDIDATE = re.compile(r"[A-Za-z0-9+/_\-]{32,}")


def _entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values())


def _looks_secret(token: str) -> bool:
    mixed = (
        any(c.islower() for c in token)
        and any(c.isupper() for c in token)
        and any(c.isdigit() for c in token)
    )
    return mixed and _entropy(token) >= 4.0


class Redactor:
    def __init__(self, secrets: list[str] | None = None) -> None:
        self._values: set[str] = set()
        for s in secrets or []:
            self.register(s)

    def register(self, value: str) -> None:
        # very short values would mask ordinary words; they are still never logged by design
        if len(value) >= 6:
            self._values.add(value)

    def redact(self, text: str) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, MASK)
        for pattern in _PATTERNS:
            text = pattern.sub(MASK, text)
        return _CANDIDATE.sub(lambda m: MASK if _looks_secret(m.group(0)) else m.group(0), text)

    def redact_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {self.redact_obj(k): self.redact_obj(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.redact_obj(v) for v in obj]
        return obj
