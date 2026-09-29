"""Email: SMTP out, an inbound-parse webhook in.

Credentials (a secret) are JSON: ``{"host", "port", "username", "password", "from",
"security": "starttls" | "ssl" | "none", "inbound_secret"}``, or a URL
``smtp://user:pass@host:587?from=bot@example.com&inbound_secret=...`` (``smtps://`` for SSL).
The channel's ``address`` is the From address when the credentials do not give one.

Inbound mail reaches ``POST /channels/{name}`` from the mail provider's inbound-parse
webhook (SendGrid, Mailgun, SES via a small forwarder, ...) with
``Authorization: Bearer <inbound_secret>``: either JSON ``{"from", "subject", "text",
"message_id"}`` or the raw message (``message/rfc822``). Quoted earlier messages are cut, so
the agent sees what the contact wrote. Without an ``inbound_secret`` the channel only sends.

Replies keep the thread: ``Re: <subject>`` and ``In-Reply-To`` the contact's last message.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import re
import smtplib
from email.message import EmailMessage
from email.parser import BytesParser
from email.policy import default as default_policy
from email.utils import make_msgid, parseaddr
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from ..spec.schema import Channel
from .base import ChannelError, Envelope, Inbound, Unauthorized

_QUOTE_START = re.compile(
    r"^(On .+ wrote:|El .+ escribió:|-----\s*Original Message\s*-----|From: .+)$", re.IGNORECASE
)


def smtp_settings(credentials: Any) -> dict[str, Any]:
    if isinstance(credentials, dict):
        data = dict(credentials)
    elif isinstance(credentials, str) and credentials.strip().startswith("{"):
        data = json.loads(credentials)
    elif isinstance(credentials, str) and credentials.startswith(("smtp://", "smtps://")):
        url = urlparse(credentials)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        data = {
            "host": url.hostname,
            "port": url.port,
            "username": unquote(url.username or ""),
            "password": unquote(url.password or ""),
            "security": "ssl" if url.scheme == "smtps" else query.get("security", "starttls"),
            **{k: v for k, v in query.items() if k != "security"},
        }
    else:
        raise ChannelError("email credentials must be JSON or an smtp:// URL")
    if not data.get("host"):
        raise ChannelError("email credentials need an SMTP host")
    security = str(data.get("security") or "starttls")
    if security not in ("starttls", "ssl", "none"):
        raise ChannelError("email security must be starttls, ssl or none")
    data["security"] = security
    data["port"] = int(data.get("port") or (465 if security == "ssl" else 587))
    return data


def strip_quoted(text: str) -> str:
    """What the sender wrote, without the quoted thread below it."""
    kept: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if _QUOTE_START.match(line.strip()) and kept:
            break
        if line.lstrip().startswith(">"):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


class EmailChannel:
    inline_reply = False

    def __init__(self, name: str, config: Channel, credentials: Any) -> None:
        if credentials is None:
            raise ChannelError("email needs SMTP credentials")
        self.name = name
        self.config = config
        self.settings = smtp_settings(credentials)
        self.sender = str(self.settings.get("from") or config.address or "")
        if not self.sender:
            raise ChannelError("email needs a From address (credentials 'from' or 'address')")
        self.inbound_secret = str(self.settings.get("inbound_secret") or "")
        self.threads: dict[str, tuple[str, str | None]] = {}  # contact: (subject, message id)

    # --- inbound -----------------------------------------------------------------------

    def parse(self, request: Inbound) -> list[Envelope]:
        bearer = request.headers.get("authorization", "")
        given = bearer[7:].strip() if bearer.startswith("Bearer ") else ""
        if not self.inbound_secret or not hmac.compare_digest(given, self.inbound_secret):
            raise Unauthorized("invalid inbound email secret")
        if request.headers.get("content-type", "").startswith("application/json"):
            data = request.json() or {}
            sender, subject = str(data.get("from") or ""), str(data.get("subject") or "")
            body, message_id = str(data.get("text") or ""), data.get("message_id")
        else:
            message = BytesParser(policy=default_policy).parsebytes(request.body)
            sender, subject = str(message.get("from", "")), str(message.get("subject", ""))
            message_id = message.get("message-id")
            part = message.get_body(preferencelist=("plain",))
            body = part.get_content() if part is not None else ""
        display, address = parseaddr(sender)
        if not address or "@" not in address:
            raise ChannelError("inbound email has no sender address")
        text = strip_quoted(body)
        if not text:
            return []
        contact = address.lower()
        self.threads[contact] = (subject, str(message_id) if message_id else None)
        names = [display] if display else []
        return [
            Envelope(
                channel=self.name,
                contact_key=contact,
                text=f"{subject}\n\n{text}" if subject and not subject.lower().startswith("re:")
                else text,
                message_id=str(message_id) if message_id else None,
                names=names,
            )
        ]  # fmt: skip

    # --- outbound ----------------------------------------------------------------------

    def _message(self, to: str, text: str) -> EmailMessage:
        subject, reply_to = self.threads.get(to.lower(), ("", None))
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = to
        if subject:
            msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        else:
            msg["Subject"] = text.split("\n", 1)[0][:70] or "Message"
        if reply_to:
            msg["In-Reply-To"] = reply_to
            msg["References"] = reply_to
        msg["Message-ID"] = make_msgid(domain=self.sender.rsplit("@", 1)[-1])
        msg.set_content(text)
        return msg

    def _deliver(self, msg: EmailMessage) -> None:
        s = self.settings
        smtp: smtplib.SMTP
        if s["security"] == "ssl":
            smtp = smtplib.SMTP_SSL(s["host"], s["port"], timeout=30)
        else:
            smtp = smtplib.SMTP(s["host"], s["port"], timeout=30)
        with smtp:
            if s["security"] == "starttls":
                smtp.starttls()
            if s.get("username"):
                smtp.login(str(s["username"]), str(s.get("password") or ""))
            smtp.send_message(msg)

    async def send(self, contact_key: str, text: str) -> str | None:
        if "@" not in contact_key:
            raise ChannelError(f"not an email address: {contact_key!r}")
        msg = self._message(contact_key, text)
        try:
            await asyncio.to_thread(self._deliver, msg)
        except (OSError, smtplib.SMTPException) as exc:
            raise ChannelError(f"email send failed: {type(exc).__name__}: {exc}") from None
        return str(msg["Message-ID"])

    async def send_template(
        self, contact_key: str, template: str, variables: dict[str, str]
    ) -> str | None:
        tpl = self.config.templates.get(template)
        if tpl is None:
            raise ChannelError(f"unknown template {template!r}")
        text = Path(tpl.file).read_text(encoding="utf-8")
        for key, value in variables.items():
            text = text.replace("{{" + key + "}}", value)
        return await self.send(contact_key, text)
