"""Validation issues and the error raised when a spec cannot be loaded at all."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Issue:
    severity: Literal["error", "warning"]
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.severity}[{self.code}] {self.path}: {self.message}"


class SpecError(Exception):
    """The spec cannot be loaded (bad JSON, unknown keys, schema errors, missing pack)."""

    def __init__(self, issues: list[Issue]) -> None:
        self.issues = issues
        super().__init__("\n".join(str(i) for i in issues))
