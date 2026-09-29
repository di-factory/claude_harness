"""Messaging gateway (Twilio-style) for WhatsApp and SMS.

The client holds the gateway account (decision 30): the credentials are theirs, resolved
from their vault as ``{"account_sid", "auth_token"}`` or ``"SID:TOKEN"``.

Inbound: Twilio posts form fields (``From``, ``Body``, ``MessageSid``, ``ProfileName``) and
signs them: ``X-Twilio-Signature`` = base64(HMAC-SHA1(auth_token, url + sorted key+value)).
The URL must be the public one the gateway called (``public_url``), since a load balancer
changes what the service sees.

Outbound: the Messages API; templates (required outside WhatsApp's 24 h window) are sent by
``provider_template_id`` (``ContentSid``) with ``ContentVariables``.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import httpx2

from ..spec.schema import Channel
from .base import ChannelError, Envelope, Inbound, Unauthorized, credential_pair

API = "https://api.twilio.com/2010-04-01"


def signature(auth_token: str, url: str, params: dict[str, str]) -> str:
    payload = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


class GatewayChannel:
    inline_reply = False

    def __init__(
        self,
        name: str,
        config: Channel,
        credentials: Any,
        *,
        client: httpx2.AsyncClient | None = None,
        public_url: str | None = None,
    ) -> None:
        if config.provider not in {None, "twilio"}:
            raise ChannelError(f"gateway provider {config.provider!r} is not supported yet")
        if not config.address:
            raise ChannelError(f"channel {name!r} needs an address (the sending number)")
        self.name = name
        self.config = config
        self.account_sid, self.auth_token = credential_pair(
            credentials, "account_sid", "auth_token"
        )
        self.http = client or httpx2.AsyncClient(timeout=20.0)
        self.public_url = public_url
        self.whatsapp = config.address.startswith("whatsapp:") or "whatsapp" in name

    def _address(self, number: str) -> str:
        bare = number.removeprefix("whatsapp:")
        return f"whatsapp:{bare}" if self.whatsapp else bare

    def parse(self, request: Inbound) -> list[Envelope]:
        params = request.form()
        url = self.public_url or request.url
        given = request.headers.get("x-twilio-signature", "")
        if not given or not hmac.compare_digest(given, signature(self.auth_token, url, params)):
            raise Unauthorized("invalid gateway signature")
        sender = params.get("From", "")
        if not sender:
            return []  # delivery status callbacks carry no sender message
        media = int(params.get("NumMedia", "0") or 0)
        return [
            Envelope(
                channel=self.name,
                contact_key=sender.removeprefix("whatsapp:"),
                text=params.get("Body", ""),
                message_id=params.get("MessageSid") or None,
                names=[params["ProfileName"]] if params.get("ProfileName") else [],
                attachments=[
                    {"url": params.get(f"MediaUrl{i}"), "type": params.get(f"MediaContentType{i}")}
                    for i in range(media)
                ],
            )
        ]

    async def _post(self, data: dict[str, str]) -> str | None:
        response = await self.http.post(
            f"{API}/Accounts/{self.account_sid}/Messages.json",
            data=data,
            auth=(self.account_sid, self.auth_token),
        )
        if response.status_code >= 400:
            raise ChannelError(
                f"gateway send failed: HTTP {response.status_code}: {response.text[:300]}"
            )
        sid = response.json().get("sid")
        return str(sid) if sid else None

    async def send(self, contact_key: str, text: str) -> str | None:
        return await self._post(
            {
                "From": self._address(self.config.address or ""),
                "To": self._address(contact_key),
                "Body": text,
            }
        )

    async def send_template(
        self, contact_key: str, template: str, variables: dict[str, str]
    ) -> str | None:
        tpl = self.config.templates.get(template)
        if tpl is None or not tpl.provider_template_id:
            raise ChannelError(f"template {template!r} has no provider_template_id")
        return await self._post(
            {
                "From": self._address(self.config.address or ""),
                "To": self._address(contact_key),
                "ContentSid": tpl.provider_template_id,
                "ContentVariables": json.dumps(variables, ensure_ascii=False),
            }
        )
