"""The REST / web channel: a client system posts a message and gets the reply back.

``POST`` JSON ``{"contact": "...", "text": "...", "message_id"?: "...", "name"?: "..."}``
with ``Authorization: Bearer <token>``. The token is the channel's credentials (resolved from
the vault); a channel without one refuses every request rather than run open.
"""

from __future__ import annotations

import hmac
from typing import Any

from ..spec.schema import Channel
from .base import ChannelError, Envelope, Inbound, Unauthorized


class ApiChannel:
    inline_reply = True

    def __init__(self, name: str, config: Channel, credentials: Any) -> None:
        self.name = name
        self.config = config
        self.token = credentials if isinstance(credentials, str) and credentials else None

    def parse(self, request: Inbound) -> list[Envelope]:
        auth = request.headers.get("authorization", "")
        given = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if self.token is None or not given or not hmac.compare_digest(given, self.token):
            raise Unauthorized("invalid or missing bearer token")
        body = request.json()
        if (
            not isinstance(body, dict)
            or not body.get("contact")
            or not isinstance(body.get("text"), str)
        ):
            raise ChannelError("expected JSON with 'contact' and 'text'")
        return [
            Envelope(
                channel=self.name,
                contact_key=str(body["contact"]),
                text=body["text"],
                message_id=str(body["message_id"]) if body.get("message_id") else None,
                names=[str(body["name"])] if body.get("name") else [],
            )
        ]

    async def send(self, contact_key: str, text: str) -> str | None:
        return None  # replies go back in the HTTP response

    async def send_template(
        self, contact_key: str, template: str, variables: dict[str, str]
    ) -> str | None:
        return None
