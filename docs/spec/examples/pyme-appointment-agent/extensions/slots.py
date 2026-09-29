"""Clinic-specific slot ranking: an example extension (``tools.python``).

It is a plain function with type hints, so it becomes the read tool ``slots.best_slot``.
"""

from __future__ import annotations

from typing import Any


async def best_slot(
    slots: list[dict[str, Any]],
    prefer_calendar: str | None = None,
    not_before: str | None = None,
) -> dict[str, Any] | None:
    """Pick the slot to offer first from calendar.find_slots results: the patient's usual
    practitioner (prefer_calendar) when free, else the earliest; not_before is HH:MM."""
    usable = [s for s in slots if not not_before or s["start"][11:16] >= not_before]
    preferred = [s for s in usable if s.get("calendar_id") == prefer_calendar]
    ranked = sorted(preferred or usable, key=lambda s: s["start"])
    return ranked[0] if ranked else None
