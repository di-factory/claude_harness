"""Channel adapters (ARCHITECTURE §3.15): every inbound message becomes one ``Envelope``.

An adapter verifies that an inbound request really comes from its provider (signature or
shared secret), parses it into envelopes, and sends outbound text and templates. The rest
of the runtime never sees provider formats.

Contact keys are the provider's stable id for a person (phone number, Telegram user id,
email), so sessions and consent follow the person across messages.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..spec.schema import Channel


class ChannelError(RuntimeError):
    pass


class Unauthorized(ChannelError):
    """The request is not from the provider it claims (bad or missing signature)."""


class Handshake(ChannelError):
    """A verified provider request that wants a fixed answer, not a conversation (Slack's
    URL check). The service answers ``body`` with HTTP 200."""

    def __init__(self, body: dict[str, Any]) -> None:
        super().__init__("handshake")
        self.body = body


@dataclass(frozen=True)
class Envelope:
    channel: str
    contact_key: str
    text: str
    message_id: str | None = None
    names: list[str] = field(default_factory=list)  # the contact's profile names, for PII
    attachments: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class Inbound:
    """An HTTP request as the service received it."""

    url: str
    headers: Mapping[str, str]  # lower-case names
    body: bytes
    client: str = ""  # the peer address (behind the proxy: X-Forwarded-For is used instead)

    def form(self) -> dict[str, str]:
        from urllib.parse import parse_qsl

        return dict(parse_qsl(self.body.decode("utf-8"), keep_blank_values=True))

    def json(self) -> Any:
        try:
            return json.loads(self.body or b"null")
        except ValueError as exc:
            raise ChannelError(f"invalid JSON body: {exc}") from None


class ChannelAdapter(Protocol):
    name: str
    config: Channel
    inline_reply: bool  # True: the reply goes back in the HTTP response (REST, web)

    def parse(self, request: Inbound) -> list[Envelope]:
        """Verify and parse; raises ``Unauthorized`` for a request that fails verification."""
        ...

    async def send(self, contact_key: str, text: str) -> str | None:
        """Send text; returns the provider's message id when it gives one."""
        ...

    async def send_template(
        self, contact_key: str, template: str, variables: dict[str, str]
    ) -> str | None: ...


def credential_pair(value: Any, first: str, second: str) -> tuple[str, str]:
    """A two-part credential from a resolved secret: JSON ``{first, second}`` or ``a:b``."""
    if isinstance(value, dict):
        data = value
    elif isinstance(value, str) and value.strip().startswith("{"):
        data = json.loads(value)
    elif isinstance(value, str) and ":" in value:
        a, _, b = value.partition(":")
        return a.strip(), b.strip()
    else:
        raise ChannelError(f"credentials must hold {first} and {second}")
    if not data.get(first) or not data.get(second):
        raise ChannelError(f"credentials must hold {first} and {second}")
    return str(data[first]), str(data[second])
