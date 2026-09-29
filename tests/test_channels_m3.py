"""Email and Slack channels (M3.8)."""

from __future__ import annotations

import json
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Any, ClassVar

import pytest

from dif_general_harness.channels import ChannelError
from dif_general_harness.channels.email import smtp_settings, strip_quoted
from dif_general_harness.channels.slack import slack_signature
from dif_general_harness.core.messages import Message
from tests.support import Env

SMTP_URL = "smtp://bot:p%40ss@smtp.example.com:587?from=desk@acme.mx&inbound_secret=mail-in"
SLACK = "xoxb-1-2-abc:slack-signing-secret"


class FakeSMTP:
    sent: ClassVar[list[EmailMessage]] = []
    calls: ClassVar[list[str]] = []

    def __init__(self, host: str, port: int, timeout: float = 0) -> None:
        self.calls.append(f"connect {host}:{port}")

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> None:
        self.calls.append("quit")

    def starttls(self) -> None:
        self.calls.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.calls.append(f"login {user}:{password}")

    def send_message(self, msg: EmailMessage) -> None:
        self.sent.append(msg)


@pytest.fixture
def smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    FakeSMTP.sent, FakeSMTP.calls = [], []
    monkeypatch.setattr("dif_general_harness.channels.email.smtplib.SMTP", FakeSMTP)
    return FakeSMTP


def _mail(spec: dict[str, Any]) -> None:
    spec["secrets"]["smtp"] = {"description": "SMTP"}
    spec["channels"]["mail"] = {
        "type": "email",
        "credentials": {"$secret": "smtp"},
        "entry_agent": "front",
        "contact_key": "email",
    }
    spec["policies"] = {"escalation": {"handoff_to": {"type": "email", "to": "staff@acme.mx"}}}


def test_smtp_settings_and_quote_stripping() -> None:
    s = smtp_settings(SMTP_URL)
    assert (s["host"], s["port"], s["password"], s["security"]) == (
        "smtp.example.com",
        587,
        "p@ss",
        "starttls",
    )
    assert smtp_settings({"host": "h", "security": "ssl"})["port"] == 465
    with pytest.raises(ChannelError):
        smtp_settings("mailto:x")
    text = "Sí, a las 10.\n\nEl lun, 1 sep 2026, Clínica escribió:\n> ¿Confirma?\n"
    assert strip_quoted(text) == "Sí, a las 10."


async def test_email_in_and_threaded_reply(tmp_path: Path, smtp: type[FakeSMTP]) -> None:
    script = [Message.assistant("Claro, ¿qué día le queda mejor?"), Message.assistant("Listo.")]
    env = Env(tmp_path, script, edit=_mail, secrets={"smtp": SMTP_URL})
    inst, headless, client = await env.open()
    async with inst, client:
        raw = (
            b"From: Ana Perez <Ana@Example.com>\r\nTo: desk@acme.mx\r\nSubject: Cita\r\n"
            b"Message-ID: <m1@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Quiero cambiar mi cita.\r\n\r\nOn Mon, Desk wrote:\r\n> Su cita es el lunes\r\n"
        )
        auth = {"authorization": "Bearer mail-in", "content-type": "message/rfc822"}
        assert (await client.post("/channels/mail", content=raw)).status_code == 401
        r = await client.post("/channels/mail", content=raw, headers=auth)
        assert r.status_code == 200
        await headless.worker().drain()

        assert env.provider.requests[0].messages[-1].text() == "Cita\n\nQuiero cambiar mi cita."
        [reply] = smtp.sent
        assert reply["To"] == "ana@example.com" and reply["Subject"] == "Re: Cita"
        assert reply["In-Reply-To"] == "<m1@example.com>" and reply["From"] == "desk@acme.mx"
        assert reply.get_content().strip() == "Claro, ¿qué día le queda mejor?"
        assert smtp.calls[:3] == ["connect smtp.example.com:587", "starttls", "login bot:p@ss"]

        body = {"from": "ana@example.com", "subject": "Re: Cita", "text": "El martes.",
                "message_id": "<m2@example.com>"}  # fmt: skip
        json_auth = {"authorization": "Bearer mail-in", "content-type": "application/json"}
        await client.post("/channels/mail", content=json.dumps(body), headers=json_auth)
        await headless.worker().drain()
        assert env.provider.requests[1].messages[-1].text() == "El martes."
        assert len(env.provider.requests[1].messages) == 3  # the same conversation

        # escalation handoff_to email: the staff address is told through the email channel
        await headless.notify("abc123", "Escalation: angry patient")
        assert smtp.sent[-1]["To"] == "staff@acme.mx"
        assert "[inbox abc123]" in smtp.sent[-1].get_content()


def _slack(spec: dict[str, Any]) -> None:
    spec["secrets"]["slack"] = {"description": "Slack"}
    spec["channels"]["team"] = {
        "type": "slack",
        "credentials": {"$secret": "slack"},
        "entry_agent": "ops",
    }


def _signed(payload: dict[str, Any], *, stamp: int | None = None) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload).encode()
    ts = str(stamp if stamp is not None else int(time.time()))
    return body, {
        "content-type": "application/json",
        "x-slack-request-timestamp": ts,
        "x-slack-signature": slack_signature("slack-signing-secret", ts, body),
    }


async def test_slack_events_and_threads(tmp_path: Path) -> None:
    env = Env(tmp_path, [Message.assistant("3 tickets open.")], edit=_slack,
              secrets={"slack": SLACK})  # fmt: skip
    inst, headless, client = await env.open()
    async with inst, client:
        body, headers = _signed({"type": "url_verification", "challenge": "c-123"})
        r = await client.post("/channels/team", content=body, headers=headers)
        assert r.json() == {"challenge": "c-123"}

        stale_body, stale = _signed({"type": "event_callback"}, stamp=int(time.time()) - 900)
        assert (
            await client.post("/channels/team", content=stale_body, headers=stale)
        ).status_code == 401
        headers["x-slack-signature"] = "v0=bad"
        assert (
            await client.post("/channels/team", content=body, headers=headers)
        ).status_code == 401

        mention = {
            "type": "event_callback",
            "event_id": "Ev1",
            "event": {"type": "app_mention", "channel": "C9", "user": "U1", "ts": "171.5",
                      "text": "<@UBOT> how many open tickets?"},
        }  # fmt: skip
        own = {
            "type": "event_callback",
            "event_id": "Ev2",
            "event": {"type": "message", "channel": "D3", "bot_id": "B1", "text": "echo"},
        }
        for payload in (mention, mention, own):  # Slack retries: the same event runs once
            body, headers = _signed(payload)
            assert (
                await client.post("/channels/team", content=body, headers=headers)
            ).status_code == 200
        await headless.worker().drain()

        assert len(env.provider.requests) == 1
        assert env.provider.requests[0].messages[-1].text() == "how many open tickets?"
        [post] = [r for r in env.sent if "slack.com" in r.url.host]
        assert post.url.path == "/api/chat.postMessage"
        assert post.headers["authorization"] == "Bearer xoxb-1-2-abc"
        assert json.loads(post.content) == {"channel": "C9", "text": "3 tickets open.",
                                            "thread_ts": "171.5"}  # fmt: skip
