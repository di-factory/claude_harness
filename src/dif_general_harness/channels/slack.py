"""Slack: the Events API in, ``chat.postMessage`` out.

Credentials (a secret): ``{"bot_token", "signing_secret"}`` or ``bot_token:signing_secret``.
Point the Slack app's Event Subscriptions at ``POST /channels/{name}`` and subscribe to
``message.im`` and ``app_mention``. Requests are verified with the signing secret (``v0``
HMAC over timestamp and body, at most five minutes old); Slack's URL check is answered.

Contact keys: a direct message is keyed by its DM channel (``D...``); a mention in a channel
by the channel and thread (``C.../<thread ts>``), so every thread is its own conversation and
replies go into it. The bot's own messages and edits are ignored.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from pathlib import Path
from typing import Any

import httpx2

from ..spec.schema import Channel
from .base import ChannelError, Envelope, Handshake, Inbound, Unauthorized, credential_pair

API = "https://slack.com/api"
MAX_SKEW_S = 300
_MENTION = re.compile(r"<@[A-Z0-9]+>\s*")


def slack_signature(secret: str, timestamp: str, body: bytes) -> str:
    base = b"v0:" + timestamp.encode() + b":" + body
    return "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()


class SlackChannel:
    inline_reply = False

    def __init__(
        self,
        name: str,
        config: Channel,
        credentials: Any,
        *,
        client: httpx2.AsyncClient | None = None,
        clock: Any = time.time,
    ) -> None:
        self.name = name
        self.config = config
        self.token, self.secret = credential_pair(credentials, "bot_token", "signing_secret")
        self.http = client or httpx2.AsyncClient(timeout=20.0)
        self.clock = clock

    def parse(self, request: Inbound) -> list[Envelope]:
        stamp = request.headers.get("x-slack-request-timestamp", "")
        given = request.headers.get("x-slack-signature", "")
        if not stamp.isdigit() or abs(self.clock() - int(stamp)) > MAX_SKEW_S:
            raise Unauthorized("stale or missing slack timestamp")
        if not hmac.compare_digest(given, slack_signature(self.secret, stamp, request.body)):
            raise Unauthorized("invalid slack signature")
        payload = request.json() or {}
        if payload.get("type") == "url_verification":
            raise Handshake({"challenge": payload.get("challenge", "")})
        event = payload.get("event") or {}
        if payload.get("type") != "event_callback" or event.get("type") not in (
            "message",
            "app_mention",
        ):
            return []
        if event.get("bot_id") or event.get("subtype") or not event.get("text"):
            return []  # our own posts, edits, joins
        channel = str(event.get("channel") or "")
        if event.get("channel_type") == "im" or channel.startswith("D"):
            contact = channel
        elif event["type"] == "app_mention":
            contact = f"{channel}/{event.get('thread_ts') or event.get('ts')}"
        else:
            return []  # channel chatter that does not mention the bot
        return [
            Envelope(
                channel=self.name,
                contact_key=contact,
                text=_MENTION.sub("", str(event["text"])).strip(),
                message_id=str(payload.get("event_id") or event.get("client_msg_id") or "") or None,
            )
        ]

    async def send(self, contact_key: str, text: str) -> str | None:
        channel, _, thread = contact_key.partition("/")
        body: dict[str, Any] = {"channel": channel, "text": text}
        if thread:
            body["thread_ts"] = thread
        response = await self.http.post(
            f"{API}/chat.postMessage",
            json=body,
            headers={"authorization": f"Bearer {self.token}"},
        )
        data = response.json() if response.content else {}
        if response.status_code >= 400 or not data.get("ok", False):
            raise ChannelError(f"slack send failed: {data.get('error') or response.status_code}")
        return str(data.get("ts") or "") or None

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
