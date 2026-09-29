"""Tenant scope carried by every record (ARCHITECTURE §3.14)."""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, field_validator

_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")


class Scope(BaseModel):
    """Who a record belongs to. Every session, event and store entry carries one."""

    model_config = ConfigDict(frozen=True)

    tenant_id: str
    instance_id: str

    @field_validator("tenant_id", "instance_id")
    @classmethod
    def _valid_id(cls, value: str) -> str:
        if not _ID_RE.match(value):
            raise ValueError(
                f"invalid id {value!r}: use lowercase letters, digits, '.', '_' or '-' "
                "(max 63 chars, starting with a letter or digit)"
            )
        return value

    def key(self) -> str:
        """Stable string used for namespacing storage paths and keys."""
        return f"{self.tenant_id}/{self.instance_id}"
