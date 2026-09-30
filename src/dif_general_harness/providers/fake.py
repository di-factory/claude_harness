"""FakeProvider: replays scripted assistant turns for deterministic, offline tests."""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Callable, Sequence

from ..core.messages import Message, Role, TextBlock, Usage
from .base import Embeddings, ModelRequest, ProviderEvent, ProviderMessage, ProviderTextDelta

Step = Message | ProviderMessage | Exception | Callable[[ModelRequest], Message]


def hashed_embedding(text: str, dim: int = 64) -> list[float]:
    """A deterministic bag-of-words vector (tests): shared words mean similar vectors."""
    vector = [0.0] * dim
    for word in text.lower().split():
        vector[int(hashlib.sha256(word.encode()).hexdigest(), 16) % dim] += 1.0
    return vector


class ScriptError(RuntimeError):
    """The script ran out, or a step failed on purpose."""


class FakeProvider:
    """Each call to ``stream`` plays the next script step.

    A step is an assistant ``Message`` (stop reason inferred), a full ``ProviderMessage``
    (to script stop reasons, usage or the serving model), an ``Exception`` to raise, or a
    function of the request returning a ``Message`` (to answer from what a tool returned).
    Every request is recorded in ``requests`` so tests can assert on what the model saw.
    """

    name = "fake"

    def __init__(
        self,
        script: Sequence[Step],
        *,
        chunk: int = 16,
        embed: Callable[[str], list[float]] | None = None,
    ) -> None:
        self._script = list(script)
        self._chunk = chunk
        self.requests: list[ModelRequest] = []
        self._embed = embed or hashed_embedding
        self.embedded: list[list[str]] = []  # every embedding batch, for assertions

    async def embed(self, texts: list[str], *, model_role: str = "embedding") -> Embeddings:
        self.embedded.append(list(texts))
        tokens = sum(len(t) for t in texts) // 4
        return Embeddings([self._embed(t) for t in texts], "fake-embedding", tokens)

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        if not self._script:
            raise ScriptError("FakeProvider script exhausted")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        if not isinstance(step, Message | ProviderMessage):
            step = step(request)
        if isinstance(step, Message):
            prompt_size = sum(len(m.model_dump_json()) for m in request.messages)
            step = ProviderMessage(
                message=step,
                usage=Usage(
                    input_tokens=prompt_size // 4, output_tokens=len(step.model_dump_json()) // 4
                ),
                stop_reason="tool_use" if step.tool_uses() else "end_turn",
                model="fake-model",
            )
        if step.message.role is not Role.ASSISTANT:
            raise ScriptError("scripted messages must have the assistant role")
        for block in step.message.content:
            if isinstance(block, TextBlock):
                for i in range(0, len(block.text), self._chunk):
                    yield ProviderTextDelta(block.text[i : i + self._chunk])
        yield step
