"""Channel adapters: gateway (WhatsApp/SMS), Telegram, REST/web."""

from typing import Any

import httpx2

from ..spec.schema import Channel
from .api import ApiChannel
from .base import ChannelAdapter, ChannelError, Envelope, Inbound, Unauthorized
from .gateway import GatewayChannel
from .telegram import TelegramChannel


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
    if config.type in {"api", "web"}:
        return ApiChannel(name, config, credentials)
    raise ChannelError(f"channel type {config.type!r} is not supported yet")


__all__ = [
    "ApiChannel",
    "ChannelAdapter",
    "ChannelError",
    "Envelope",
    "GatewayChannel",
    "Inbound",
    "TelegramChannel",
    "Unauthorized",
    "build_adapter",
]
