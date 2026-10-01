"""Per-role model routing (decision 11): each model role maps to a provider and model.

``models.providers`` holds provider settings with ``$secret`` references already resolved.
Roles an instance does not define fall back to ``main``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from ..providers.base import Embeddings, ModelProvider, ModelRequest, ProviderEvent
from ..spec.schema import ModelRoleConfig

ProviderFactory = Callable[[ModelRoleConfig, dict[str, Any]], ModelProvider]


class RoutingError(ValueError):
    pass


def check_anthropic_key(key: Any, settings: dict[str, Any]) -> None:
    """Refuse, at start, Anthropic credentials that can never run the harness (a Claude
    subscription's OAuth token), with the fix. Whether an API key is tied to a workspace
    cannot be told from its text: the API says so on the first call, and the provider turns
    that answer into the same kind of fix."""
    if not isinstance(key, str) or settings.get("base_url"):
        return  # a gateway decides what it accepts
    if key.startswith("sk-ant-oat"):
        raise RoutingError(
            "the anthropic secret is a Claude subscription (OAuth) token; subscriptions cannot"
            " run applications. Create an API key at console.anthropic.com (API Keys)"
        )


def _anthropic(role: ModelRoleConfig, settings: dict[str, Any]) -> ModelProvider:
    from ..providers.anthropic import AnthropicProvider

    check_anthropic_key(settings.get("api_key"), settings)
    return AnthropicProvider(
        role.model,
        api_key=settings.get("api_key"),
        base_url=settings.get("base_url"),
        effort=role.effort,
        workspace_id=settings.get("workspace_id"),
    )


def _openai_compatible(role: ModelRoleConfig, settings: dict[str, Any]) -> ModelProvider:
    from ..providers.openai_compat import OpenAICompatibleProvider

    return OpenAICompatibleProvider(
        role.model,
        api_key=settings.get("api_key"),
        base_url=settings.get("base_url"),
        effort=role.effort,
    )


DEFAULT_FACTORIES: dict[str, ProviderFactory] = {
    "anthropic": _anthropic,
    "openai": _openai_compatible,
    "openai-compatible": _openai_compatible,
}


class RoleRouter:
    """A ``ModelProvider`` that sends each request to the provider of its model role."""

    name = "router"

    def __init__(self, providers: dict[str, ModelProvider]) -> None:
        if "main" not in providers:
            raise RoutingError("models.roles must define 'main'")
        self.providers = providers

    def for_role(self, role: str) -> ModelProvider:
        return self.providers.get(role) or self.providers["main"]

    def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]:
        return self.for_role(request.model_role).stream(request)

    async def embed(self, texts: list[str], *, model_role: str = "embedding") -> Embeddings:
        provider = self.providers.get(model_role)  # never falls back to a chat model
        embed = getattr(provider, "embed", None)
        if embed is None:
            raise RoutingError(f"model role {model_role!r} has no embedding model")
        result: Embeddings = await embed(texts, model_role=model_role)
        return result


def build_router(
    roles: dict[str, ModelRoleConfig],
    providers: dict[str, dict[str, Any]],
    factories: dict[str, ProviderFactory] | None = None,
) -> RoleRouter:
    table = {**DEFAULT_FACTORIES, **(factories or {})}
    built: dict[str, ModelProvider] = {}
    for name, role in roles.items():
        if not role.model or role.model.startswith(("<", "{{")):
            raise RoutingError(f"models.roles.{name}: model is not set ({role.model!r})")
        settings = providers.get(role.provider, {})
        via = settings.get("via", "direct")
        if via != "direct":
            raise RoutingError(f"provider {role.provider!r}: via {via!r} is not supported yet")
        factory = table.get(role.provider)
        if factory is None:
            raise RoutingError(f"models.roles.{name}: unknown provider {role.provider!r}")
        built[name] = factory(role, settings)
    return RoleRouter(built)
