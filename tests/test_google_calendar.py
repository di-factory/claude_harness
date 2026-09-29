"""The Google Calendar connector pack (M3.8), against an in-process fake of Google's API."""

from __future__ import annotations

import base64
import json
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote
from zoneinfo import ZoneInfo

import httpx2
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from dif_general_harness.core.messages import Message, ToolStatus, ToolUseBlock
from dif_general_harness.tools.packs.google_calendar import CalendarError, contact_of, parse_hours
from tests.support import ANA, Env, whatsapp

TZ = ZoneInfo("America/Mexico_City")
DRA, DR = "dra@clinic.example", "dr@clinic.example"


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class FakeGoogle:
    """Token endpoint (checks the JWT signature), freeBusy and events, in memory."""

    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.events: dict[str, dict[str, dict[str, Any]]] = {DRA: {}, DR: {}}
        self.token_requests = 0

    def service_account(self) -> str:
        pem = self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ).decode()
        return json.dumps(
            {
                "type": "service_account",
                "client_email": "bot@p.iam.example",
                "private_key": pem,
                "private_key_id": "k1",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )

    def add(self, cal: str, start: datetime, minutes: int = 30, **extra: Any) -> str:
        event_id = uuid.uuid4().hex[:8]
        self.events[cal][event_id] = {
            "id": event_id,
            "status": "confirmed",
            "summary": extra.pop("summary", "Cita"),
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": (start + timedelta(minutes=minutes)).isoformat()},
            **extra,
        }
        return event_id

    @staticmethod
    def _span(event: dict[str, Any]) -> tuple[datetime, datetime]:
        return (
            datetime.fromisoformat(event["start"]["dateTime"]),
            datetime.fromisoformat(event["end"]["dateTime"]),
        )

    def __call__(self, request: httpx2.Request) -> httpx2.Response | None:
        if request.url.host == "oauth2.googleapis.com":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            head, claims, sig = form["assertion"].split(".")
            self.key.public_key().verify(  # raises on a bad signature
                _unb64(sig), f"{head}.{claims}".encode(), padding.PKCS1v15(), hashes.SHA256()
            )
            assert json.loads(_unb64(claims))["iss"] == "bot@p.iam.example"
            self.token_requests += 1
            return httpx2.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        if request.url.host != "www.googleapis.com":
            return None
        if request.headers.get("authorization") != "Bearer tok-1":
            return httpx2.Response(401, json={"error": {"message": "no token"}})
        parts = [unquote(p) for p in request.url.raw_path.decode().split("?")[0].split("/")]
        parts = parts[3:]  # after /calendar/v3
        if parts == ["freeBusy"]:
            body = json.loads(request.content)
            lo, hi = (datetime.fromisoformat(body[k]) for k in ("timeMin", "timeMax"))
            out = {}
            for item in body["items"]:
                spans = [
                    self._span(e)
                    for e in self.events[item["id"]].values()
                    if e["status"] != "cancelled"
                ]
                out[item["id"]] = {
                    "busy": [
                        {"start": s.isoformat(), "end": e.isoformat()}
                        for s, e in spans
                        if s < hi and lo < e
                    ]
                }
            return httpx2.Response(200, json={"calendars": out})
        cal = self.events[parts[1]]
        if len(parts) == 3 and request.method == "GET":
            q = dict(request.url.params)
            lo, hi = (datetime.fromisoformat(q[k]) for k in ("timeMin", "timeMax"))
            items = sorted(
                (e for e in cal.values() if self._span(e)[0] < hi and lo < self._span(e)[1]),
                key=lambda e: self._span(e)[0],
            )
            return httpx2.Response(200, json={"items": items})
        if len(parts) == 3 and request.method == "POST":
            body = json.loads(request.content)
            event_id = uuid.uuid4().hex[:8]
            cal[event_id] = {"id": event_id, "status": "confirmed", **body}
            return httpx2.Response(200, json=cal[event_id])
        event = cal.get(parts[3])
        if event is None:
            return httpx2.Response(404, json={"error": {"message": "Not Found"}})
        if request.method == "PATCH":
            event.update(json.loads(request.content))
        elif request.method == "DELETE":
            del cal[parts[3]]
            return httpx2.Response(204)
        return httpx2.Response(200, json=event)


def _next_weekday(days_ahead: int = 3) -> datetime:
    day = datetime.now(TZ).date() + timedelta(days=days_ahead)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return datetime.combine(day, datetime.min.time(), TZ)


def _calendar(spec: dict[str, Any]) -> None:
    spec["secrets"]["google"] = {"description": "service account"}
    spec["tools"]["packs"] = ["general", "connectors/google-calendar"]
    spec["tools"]["config"] = {
        "connectors/google-calendar": {
            "credentials": {"$secret": "google"},
            "calendar_ids": [DRA, DR],
            "business_hours": {"mon-fri": "09:00-12:00"},
            "slot_minutes": 60,
        }
    }
    spec["tools"]["overrides"] = {"calendar.move_event": {"verify": "no-double-booking"}}
    spec["agents"]["front"]["tools"] = ["notes.*", "calendar.*"]
    spec["policies"] = {
        "permissions": {"allow": ["calendar.*"], "deny": ["calendar.delete_event"]},
        "verification": {
            "checks": {
                "no-double-booking": {
                    "type": "tool",
                    "tool": "calendar.check_conflicts",
                    "expect": "no_conflicts",
                }
            }
        },
    }
    spec["triggers"]["reminder"] = {
        "type": "relative",
        "source": "calendar.events",
        "offset": "-24h",
        "workflow": "remind",
        "requires_consent": True,
    }
    spec["workflows"]["remind"] = {
        "steps": [
            {
                "id": "msg",
                "type": "message",
                "channel": "whatsapp",
                "to": "{{event.contact}}",
                "text": "Recordatorio: {{event.summary}}",
            }
        ]
    }


def _env(tmp_path: Path, google: FakeGoogle, script: list[Any]) -> Env:
    return Env(
        tmp_path,
        script,
        edit=_calendar,
        secrets={"google": google.service_account()},
        routes=google,
    )


def test_hours_and_contacts() -> None:
    assert parse_hours({"mon-fri": "09:00-13:00,15:00-19:00", "sat": "09:00-14:00"}) == {
        **{d: [(540, 780), (900, 1140)] for d in range(5)},
        5: [(540, 840)],
    }
    with pytest.raises(CalendarError):
        parse_hours({"weekdays": "9-5"})
    assert contact_of({"description": "Paciente: Ana, tel. +52 1 55 1234 5678"}) == ANA
    assert contact_of({"extendedProperties": {"private": {"contact": "+1555"}}}) == "+1555"


async def test_slots_conflicts_and_guarded_moves(tmp_path: Path) -> None:
    google = FakeGoogle()
    day = _next_weekday()
    booked = google.add(DRA, day.replace(hour=9), 60, summary="Limpieza Ana")
    google.add(DR, day.replace(hour=9), 60)
    google.add(DRA, day.replace(hour=10), 60)
    env = _env(tmp_path, google, [])
    inst, headless, client = await env.open()
    async with inst, client:
        tools = headless.agent("front").tools

        async def run(name: str, args: dict[str, Any]) -> Any:
            result = await tools.execute(ToolUseBlock(id="t", name=name, input=args))
            return result

        found = await run("calendar.find_slots", {"date_from": day.date().isoformat()})
        assert found.status is ToolStatus.OK
        slots = [(s["start"][11:16], s["calendar_id"]) for s in found.content["slots"]]
        assert slots == [("10:00", DR), ("11:00", DRA)]  # 9:00 is taken on both calendars

        clash = await run(
            "calendar.check_conflicts",
            {"start": day.replace(hour=10).isoformat(), "event_id": booked},
        )
        assert clash.content["no_conflicts"] is False
        assert google.token_requests == 1  # the token is cached

        # move_event is guarded by the no-double-booking check
        session = await headless.agent("front").new_session(contact_key=ANA)
        bad = ToolUseBlock(
            id="m1",
            name="calendar.move_event",
            input={"event_id": booked, "start": day.replace(hour=10).isoformat()},
        )
        verdict = await inst.verifier.verify(session, bad, tools.get("calendar.move_event"))
        assert not verdict.passed and "no_conflicts" in verdict.reason
        good = ToolUseBlock(
            id="m2",
            name="calendar.move_event",
            input={"event_id": booked, "start": day.replace(hour=11).isoformat()},
        )
        assert (await inst.verifier.verify(session, good, tools.get("calendar.move_event"))).passed
        moved = await run("calendar.move_event", good.input)
        assert moved.content["start"] == day.replace(hour=11).isoformat(timespec="minutes")
        assert google.events[DRA][booked]["end"]["dateTime"].startswith(
            day.replace(hour=12).isoformat()[:16]
        )  # the length was kept
        deleted = await headless.agent("front").gate.check(
            session,
            ToolUseBlock(id="d1", name="calendar.delete_event", input={"event_id": booked}),
        )
        assert not deleted.allowed  # the spec denies deleting


async def test_upcoming_events_drive_reminders(tmp_path: Path) -> None:
    google = FakeGoogle()
    start = datetime.now(TZ) + timedelta(hours=30)
    google.add(DRA, start, summary="Limpieza", description="Tel: +52 1 55 1234 5678")
    google.add(DR, start, summary="Sin teléfono")
    env = _env(tmp_path, google, [Message.assistant("¡Gracias!")])
    env.clock.now = time.time()
    inst, headless, client = await env.open()
    async with inst, client:
        await inst.consent.set(inst.scope, ANA, "whatsapp", "granted", "clinic import")
        await headless.start()
        await headless.worker().drain()
        rows = await inst.db.fetchall(
            "SELECT item_id FROM source_items WHERE source = ?", ("calendar.events",)
        )
        assert len(rows) == 2

        env.clock.now += 6 * 3600 + 60  # 24 h before the appointment
        await headless.worker().drain()
        [sent] = env.texts("twilio")
        assert sent["Body"] == "Recordatorio: Limpieza" and sent["To"] == f"whatsapp:{ANA}"

        body, headers = whatsapp("¡Gracias!", "SM9")
        await client.post("/channels/whatsapp", content=body, headers=headers)
        await headless.worker().drain()
        assert env.texts("twilio")[-1]["Body"] == "¡Gracias!"
