"""Telegram bots: webhook updates in, ``sendMessage`` out.

Inbound requests must carry ``X-Telegram-Bot-Api-Secret-Token``; the service registers the
webhook with ``webhook_secret`` (derived from the bot token, so nothing extra to store).
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any

import httpx2

from ..spec.schema import Channel
from .base import ChannelError, Envelope, Inbound, Unauthorized

API = "https://api.telegram.org"


def webhook_secret(bot_token: str) -> str:
    return hashlib.sha256(f"dif-telegram:{bot_token}".encode()).hexdigest()[:48]


class TelegramChannel:
    inline_reply = False

    def __init__(
        self,
        name: str,
        config: Channel,
        credentials: Any,
        *,
        client: httpx2.AsyncClient | None = None,
    ) -> None:
        if not isinstance(credentials, str) or ":" not in credentials:
            raise ChannelError("telegram credentials must be the bot token")
        self.name = name
        self.config = config
        self.token = credentials
        self.secret = webhook_secret(credentials)
        self.http = client or httpx2.AsyncClient(timeout=20.0)

    def parse(self, request: Inbound) -> list[Envelope]:
        given = request.headers.get("x-telegram-bot-api-secret-token", "")
        if not hmac.compare_digest(given, self.secret):
            raise Unauthorized("invalid telegram secret token")
        update = request.json() or {}
        message = update.get("message") or update.get("edited_message")
        if not message or "text" not in message:
            return []  # joins, stickers, edits without text: nothing to answer
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        names = [n for n in (sender.get("first_name"), sender.get("last_name")) if n]
        if len(names) == 2:
            names.append(" ".join(names))
        return [
            Envelope(
                channel=self.name,
                contact_key=str(chat.get("id") or sender.get("id")),
                text=str(message["text"]),
                message_id=f"{chat.get('id')}:{message.get('message_id')}",
                names=names,
            )
        ]

    async def send(self, contact_key: str, text: str) -> str | None:
        response = await self.http.post(
            f"{API}/bot{self.token}/sendMessage", json={"chat_id": contact_key, "text": text}
        )
        body = response.json() if response.content else {}
        if response.status_code >= 400 or not body.get("ok", False):
            raise ChannelError(f"telegram send failed: HTTP {response.status_code}")
        return str(body.get("result", {}).get("message_id", "")) or None

    async def send_template(
        self, contact_key: str, template: str, variables: dict[str, str]
    ) -> str | None:
        tpl = self.config.templates.get(template)
        if tpl is None:
            raise ChannelError(f"unknown template {template!r}")
        from pathlib import Path

        text = Path(tpl.file).read_text(encoding="utf-8")
        for key, value in variables.items():
            text = text.replace("{{" + key + "}}", value)
        return await self.send(contact_key, text)
