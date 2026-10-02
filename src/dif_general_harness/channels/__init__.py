"""Channel adapters: gateway (WhatsApp/SMS), Telegram, REST, web chat, email, Slack, voice."""

from typing import Any

import httpx2

from ..spec.schema import Channel
from .api import ApiChannel
from .base import ChannelAdapter, ChannelError, Envelope, Handshake, Inbound, Unauthorized
from .email import EmailChannel
from .gateway import GatewayChannel
from .slack import SlackChannel
from .telegram import TelegramChannel
from .voice import VoiceChannel
from .web import RateLimited, WebChannel


def build_adapter(
    name: str,
    config: Channel,
    credentials: Any,
    *,
    client: httpx2.AsyncClient | None = None,
    public_url: str | None = None,
) -> ChannelAdapter:
    if config.type == "gateway":
        return GatewayChannel(name, config, credentials, client=client, public_url=public_url)
    if config.type == "telegram":
        return TelegramChannel(name, config, credentials, client=client)
    if config.type == "web":
        return WebChannel(name, config, credentials)
    if config.type == "api":
        return ApiChannel(name, config, credentials)
    if config.type == "email":
        return EmailChannel(name, config, credentials)
    if config.type == "slack":
        return SlackChannel(name, config, credentials, client=client)
    if config.type == "voice":
        return VoiceChannel(name, config, credentials, client=client, public_url=public_url)
    raise ChannelError(f"channel type {config.type!r} is not supported yet")


__all__ = [
    "ApiChannel",
    "ChannelAdapter",
    "ChannelError",
    "EmailChannel",
    "Envelope",
    "GatewayChannel",
    "Handshake",
    "Inbound",
    "RateLimited",
    "SlackChannel",
    "TelegramChannel",
    "Unauthorized",
    "VoiceChannel",
    "WebChannel",
    "build_adapter",
]
