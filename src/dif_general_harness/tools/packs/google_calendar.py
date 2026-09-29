"""The ``connectors/google-calendar`` tool pack: Google Calendar through a service account.

Config (``tools.config["connectors/google-calendar"]``):

- ``credentials``: the service account's JSON key (a secret). Share each calendar with the
  service account's address, or set ``subject`` for domain-wide delegation.
- ``calendar_ids``: the calendars (one per practitioner); the first is the default.
- ``business_hours``: e.g. ``{"mon-fri": "09:00-19:00", "sat": "09:00-14:00"}``
  (default Monday to Friday, 09:00-18:00), in the tenant's time zone.
- ``slot_minutes`` (30), ``lookahead`` (``"14d"``: how far ahead events feed triggers),
  ``sync_every`` (``"15m"``).

Tools: ``calendar.find_slots``, ``list_events``, ``get_event`` and ``check_conflicts``
(read); ``create_event``, ``move_event``, ``cancel_event`` and ``delete_event`` (external:
they change what patients and staff see). ``check_conflicts`` answers ``no_conflicts`` so it
can guard ``move_event`` as a verification check.

It also feeds the ``calendar.events`` source that relative triggers watch (reminders): every
``sync_every`` the upcoming events are pushed as items ``{id, start, end, calendar_id,
summary, contact}``, where ``contact`` is the event's private extended property
``contact`` (or ``phone``), or the first phone number in its description.
"""

from __future__ import annotations

import base64
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx2
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ...spec.loader import duration_days
from ..registry import Effect, Tool, tool

PACK = "connectors/google-calendar"
API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
Source = Callable[[], Awaitable[list[dict[str, Any]]]]


class CalendarError(RuntimeError):
    pass


def parse_hours(spec: Any) -> dict[int, list[tuple[int, int]]]:
    """``{"mon-fri": "09:00-19:00", "sat": "09:00-14:00,16:00-18:00"}`` to weekday (0 =
    Monday) -> [(start minute, end minute)]."""
    if not spec:
        spec = {"mon-fri": "09:00-18:00"}
    if not isinstance(spec, dict):
        raise CalendarError("business_hours must map days to hours")
    out: dict[int, list[tuple[int, int]]] = {}
    for days, hours in spec.items():
        wanted: list[int] = []
        for part in str(days).lower().split(","):
            a, _, b = part.strip().partition("-")
            if a[:3] not in DAYS or (b and b[:3] not in DAYS):
                raise CalendarError(f"unknown day in business_hours: {part!r}")
            lo, hi = DAYS.index(a[:3]), DAYS.index((b or a)[:3])
            wanted += list(range(lo, hi + 1))
        spans = []
        for span in str(hours).split(","):
            match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", span)
            if not match:
                raise CalendarError(f"hours must look like 09:00-18:00, not {span!r}")
            h1, m1, h2, m2 = (int(g) for g in match.groups())
            spans.append((h1 * 60 + m1, h2 * 60 + m2))
        for day in wanted:
            out.setdefault(day, []).extend(spans)
    return out


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@dataclass
class GoogleAuth:
    """OAuth tokens for a service account (JWT bearer grant), cached until near expiry."""

    key: dict[str, Any]
    http: httpx2.AsyncClient
    subject: str | None = None
    _token: str = ""
    _expires: float = 0.0

    def assertion(self, now: float) -> str:
        claims = {
            "iss": self.key["client_email"],
            "scope": SCOPE,
            "aud": self.key.get("token_uri", "https://oauth2.googleapis.com/token"),
            "iat": int(now),
            "exp": int(now) + 3600,
        }
        if self.subject:
            claims["sub"] = self.subject
        header = {"alg": "RS256", "typ": "JWT", "kid": self.key.get("private_key_id", "")}
        signing = f"{_b64(json.dumps(header).encode())}.{_b64(json.dumps(claims).encode())}"
        private = serialization.load_pem_private_key(self.key["private_key"].encode(), None)
        if not isinstance(private, rsa.RSAPrivateKey):
            raise CalendarError("the service account key is not an RSA key")
        signature = private.sign(signing.encode(), padding.PKCS1v15(), hashes.SHA256())
        return f"{signing}.{_b64(signature)}"

    async def token(self) -> str:
        now = time.time()
        if self._token and now < self._expires - 60:
            return self._token
        response = await self.http.post(
            self.key.get("token_uri", "https://oauth2.googleapis.com/token"),
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": self.assertion(now),
            },
        )
        if response.status_code >= 400:
            raise CalendarError(f"google token request failed: HTTP {response.status_code}")
        body = response.json()
        self._token = str(body["access_token"])
        self._expires = now + float(body.get("expires_in", 3600))
        return self._token


@dataclass
class GoogleCalendar:
    auth: GoogleAuth
    calendars: list[str]
    tz: ZoneInfo
    hours: dict[int, list[tuple[int, int]]]
    slot_minutes: int = 30
    lookahead_days: float = 14
    http: httpx2.AsyncClient = field(init=False)

    def __post_init__(self) -> None:
        self.http = self.auth.http

    def calendar(self, calendar_id: str | None) -> str:
        if not calendar_id:
            return self.calendars[0]
        if calendar_id not in self.calendars:
            raise ValueError(f"unknown calendar {calendar_id!r}; known: {self.calendars}")
        return calendar_id

    async def call(self, method: str, path: str, **kw: Any) -> Any:
        headers = {"authorization": f"Bearer {await self.auth.token()}"}
        response = await self.http.request(method, f"{API}{path}", headers=headers, **kw)
        if response.status_code == 404:
            raise LookupError("not found in the calendar")
        if response.status_code >= 400:
            try:
                message = response.json().get("error", {}).get("message", "")
            except ValueError:
                message = ""
            raise CalendarError(f"google calendar HTTP {response.status_code} {message}".strip())
        return response.json() if response.content else {}

    # --- time ---------------------------------------------------------------------------

    def parse(self, value: str) -> datetime:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=self.tz)

    def iso(self, dt: datetime) -> str:
        return dt.astimezone(self.tz).isoformat(timespec="minutes")

    def times(self, event: dict[str, Any]) -> tuple[datetime, datetime]:
        def when(part: dict[str, Any]) -> datetime:
            if "dateTime" in part:
                return self.parse(part["dateTime"])
            return datetime.combine(date.fromisoformat(part["date"]), datetime.min.time(), self.tz)

        return when(event["start"]), when(event["end"])

    def summary(self, event: dict[str, Any], calendar_id: str) -> dict[str, Any]:
        start, end = self.times(event)
        return {
            "id": event["id"],
            "calendar_id": calendar_id,
            "summary": event.get("summary", ""),
            "start": self.iso(start),
            "end": self.iso(end),
            "status": event.get("status", "confirmed"),
            "contact": contact_of(event),
        }

    # --- operations ---------------------------------------------------------------------

    async def events(
        self, start: datetime, end: datetime, calendar_id: str
    ) -> list[dict[str, Any]]:
        params = {
            "timeMin": start.astimezone(UTC).isoformat(),
            "timeMax": end.astimezone(UTC).isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "250",
        }
        body = await self.call("GET", f"/calendars/{quote(calendar_id)}/events", params=params)
        return [e for e in body.get("items", []) if e.get("status") != "cancelled"]

    async def busy(
        self, start: datetime, end: datetime, calendars: list[str]
    ) -> dict[str, list[tuple[datetime, datetime]]]:
        body = await self.call(
            "POST",
            "/freeBusy",
            json={
                "timeMin": start.astimezone(UTC).isoformat(),
                "timeMax": end.astimezone(UTC).isoformat(),
                "timeZone": str(self.tz),
                "items": [{"id": c} for c in calendars],
            },
        )
        out: dict[str, list[tuple[datetime, datetime]]] = {}
        for cal in calendars:
            spans = (body.get("calendars") or {}).get(cal, {}).get("busy", [])
            out[cal] = [(self.parse(b["start"]), self.parse(b["end"])) for b in spans]
        return out

    def open_windows(self, day: date) -> list[tuple[datetime, datetime]]:
        base = datetime.combine(day, datetime.min.time(), self.tz)
        return [
            (base + timedelta(minutes=a), base + timedelta(minutes=b))
            for a, b in self.hours.get(day.weekday(), [])
        ]

    async def conflicts(
        self, start: datetime, end: datetime, calendar_id: str, exclude: str | None
    ) -> list[dict[str, Any]]:
        found = []
        for event in await self.events(start - timedelta(hours=12), end + timedelta(hours=12),
                                       calendar_id):  # fmt: skip
            if event.get("id") == exclude or event.get("transparency") == "transparent":
                continue
            s, e = self.times(event)
            if s < end and start < e:
                found.append(self.summary(event, calendar_id))
        return found

    async def upcoming(self) -> list[dict[str, Any]]:
        now = datetime.now(self.tz)
        end = now + timedelta(days=self.lookahead_days)
        items = []
        for cal in self.calendars:
            for event in await self.events(now, end, cal):
                if "dateTime" in event.get("start", {}):  # all-day events are not appointments
                    items.append(self.summary(event, cal))
        return items


def contact_of(event: dict[str, Any]) -> str | None:
    private = (event.get("extendedProperties") or {}).get("private") or {}
    for key in ("contact", "phone"):
        if private.get(key):
            return str(private[key])
    match = _PHONE.search(str(event.get("description") or ""))
    if match:
        digits = re.sub(r"[^\d+]", "", match.group(0))
        return digits if digits.startswith("+") else f"+{digits}"
    return None


def calendar_tools(
    config: dict[str, Any], tz_name: str, http: httpx2.AsyncClient
) -> tuple[list[Tool], dict[str, Source], float]:
    """The tools, the sources they feed, and how often to sync them (seconds)."""
    raw = config.get("credentials")
    key = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(key, dict) or not key.get("client_email") or not key.get("private_key"):
        raise CalendarError("credentials must be a Google service account JSON key")
    calendars = config.get("calendar_ids") or []
    if isinstance(calendars, str):
        calendars = [c.strip() for c in calendars.split(",") if c.strip()]
    if not calendars:
        raise CalendarError("calendar_ids is empty")
    cal = GoogleCalendar(
        auth=GoogleAuth(key, http, subject=config.get("subject")),
        calendars=[str(c) for c in calendars],
        tz=ZoneInfo(tz_name),
        hours=parse_hours(config.get("business_hours")),
        slot_minutes=int(config.get("slot_minutes") or 30),
        lookahead_days=duration_days(str(config.get("lookahead") or "14d")),
    )
    sync_every = duration_days(str(config.get("sync_every") or "15m")) * 86400

    @tool("calendar.find_slots")
    async def find_slots(
        date_from: str,
        date_to: str | None = None,
        duration_minutes: int | None = None,
        calendar_id: str | None = None,
        limit: int = 10,
    ) -> dict[str, Any]:
        """Free appointment slots within business hours, earliest first. Dates are
        YYYY-MM-DD in the clinic's time zone; date_to defaults to date_from. Without a
        calendar_id, every practitioner's calendar is searched."""
        first = date.fromisoformat(date_from[:10])
        last = date.fromisoformat((date_to or date_from)[:10])
        if (last - first).days > 31:
            raise ValueError("search at most 31 days at a time")
        minutes = int(duration_minutes or cal.slot_minutes)
        wanted = [cal.calendar(calendar_id)] if calendar_id else cal.calendars
        begin = datetime.combine(first, datetime.min.time(), cal.tz)
        busy = await cal.busy(begin, begin + timedelta(days=(last - first).days + 1), wanted)
        now = datetime.now(cal.tz)
        slots: list[dict[str, str]] = []
        day = first
        while day <= last and len(slots) < limit:
            for open_at, close_at in cal.open_windows(day):
                at = open_at
                while at + timedelta(minutes=minutes) <= close_at and len(slots) < limit:
                    end = at + timedelta(minutes=minutes)
                    if at > now:
                        for c in wanted:
                            if not any(s < end and at < e for s, e in busy[c]):
                                slots.append({"start": cal.iso(at), "end": cal.iso(end),
                                              "calendar_id": c})  # fmt: skip
                                break
                    at += timedelta(minutes=cal.slot_minutes)
            day += timedelta(days=1)
        return {"slots": slots, "timezone": str(cal.tz)}

    @tool("calendar.list_events")
    async def list_events(
        date_from: str, date_to: str | None = None, calendar_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Events between two dates (YYYY-MM-DD, inclusive) on one calendar."""
        first = datetime.combine(date.fromisoformat(date_from[:10]), datetime.min.time(), cal.tz)
        last = date.fromisoformat((date_to or date_from)[:10])
        end = datetime.combine(last + timedelta(days=1), datetime.min.time(), cal.tz)
        target = cal.calendar(calendar_id)
        return [cal.summary(e, target) for e in await cal.events(first, end, target)]

    @tool("calendar.get_event")
    async def get_event(event_id: str, calendar_id: str | None = None) -> dict[str, Any]:
        """One event by id."""
        target = cal.calendar(calendar_id)
        event = await cal.call("GET", f"/calendars/{quote(target)}/events/{quote(event_id)}")
        return cal.summary(event, target)

    @tool("calendar.check_conflicts")
    async def check_conflicts(
        start: str,
        end: str | None = None,
        event_id: str | None = None,
        calendar_id: str | None = None,
    ) -> dict[str, Any]:
        """Whether a time overlaps other events. For a move, give the event_id (it is not
        counted against itself; without an end its current length is kept)."""
        target = cal.calendar(calendar_id)
        begin = cal.parse(start)
        if end:
            finish = cal.parse(end)
        elif event_id:
            current = await cal.call("GET", f"/calendars/{quote(target)}/events/{quote(event_id)}")
            s, e = cal.times(current)
            finish = begin + (e - s)
        else:
            finish = begin + timedelta(minutes=cal.slot_minutes)
        found = await cal.conflicts(begin, finish, target, event_id)
        return {"no_conflicts": not found, "conflicts": found}

    @tool("calendar.create_event", effect=Effect.EXTERNAL)
    async def create_event(
        summary: str,
        start: str,
        end: str | None = None,
        contact: str | None = None,
        description: str = "",
        calendar_id: str | None = None,
    ) -> dict[str, Any]:
        """Book an appointment. contact (a phone) lets reminders reach the patient."""
        target = cal.calendar(calendar_id)
        begin = cal.parse(start)
        finish = cal.parse(end) if end else begin + timedelta(minutes=cal.slot_minutes)
        body: dict[str, Any] = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": begin.isoformat(), "timeZone": str(cal.tz)},
            "end": {"dateTime": finish.isoformat(), "timeZone": str(cal.tz)},
        }
        if contact:
            body["extendedProperties"] = {"private": {"contact": contact}}
        event = await cal.call("POST", f"/calendars/{quote(target)}/events", json=body)
        return cal.summary(event, target)

    @tool("calendar.move_event", effect=Effect.EXTERNAL)
    async def move_event(
        event_id: str, start: str, end: str | None = None, calendar_id: str | None = None
    ) -> dict[str, Any]:
        """Reschedule an event; without an end it keeps its length."""
        target = cal.calendar(calendar_id)
        path = f"/calendars/{quote(target)}/events/{quote(event_id)}"
        current = await cal.call("GET", path)
        s, e = cal.times(current)
        begin = cal.parse(start)
        finish = cal.parse(end) if end else begin + (e - s)
        event = await cal.call(
            "PATCH",
            path,
            json={
                "start": {"dateTime": begin.isoformat(), "timeZone": str(cal.tz)},
                "end": {"dateTime": finish.isoformat(), "timeZone": str(cal.tz)},
            },
        )
        return cal.summary(event, target)

    @tool("calendar.cancel_event", effect=Effect.EXTERNAL)
    async def cancel_event(event_id: str, calendar_id: str | None = None) -> str:
        """Cancel an appointment (the calendar keeps it as cancelled)."""
        target = cal.calendar(calendar_id)
        path = f"/calendars/{quote(target)}/events/{quote(event_id)}"
        await cal.call("PATCH", path, json={"status": "cancelled"})
        return f"cancelled {event_id}"

    @tool("calendar.delete_event", effect=Effect.EXTERNAL)
    async def delete_event(event_id: str, calendar_id: str | None = None) -> str:
        """Delete an event outright."""
        target = cal.calendar(calendar_id)
        await cal.call("DELETE", f"/calendars/{quote(target)}/events/{quote(event_id)}")
        return f"deleted {event_id}"

    tools = [find_slots, list_events, get_event, check_conflicts, create_event, move_event,
             cancel_event, delete_event]  # fmt: skip
    return [replace(t, source=PACK) for t in tools], {"calendar.events": cal.upcoming}, sync_every
