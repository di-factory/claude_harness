"""FakeProvider: replays scripted assistant messages for deterministic, offline tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from ..core.messages import Message, Role, TextBlock, Usage
from .base import ModelRequest, ProviderEvent, ProviderMessage, ProviderTextDelta


class ScriptError(RuntimeError):
    """The script ran out, or a step failed on purpose."""


class FakeProvider:
    """Each call to ``stream`` returns the next scripted message.

    A script step is a ``Message`` (assistant role) or an ``Exception`` to raise.
    Every request is recorded in ``requests`` so tests can assert on what the
    model was shown.
    """

    name = "fake"

    def __init__(self, script: Sequence[Message | Exception], *, chunk: int = 16) -> None:
        self._script = list(script)
        self._chunk = chunk
        self.requests: list[ModelRequest] = []

    async def stream(self, request: ModelRequest) -> AsyncIterator[ProviderEvent]:
        self.requests.append(request)
        if not self._script:
            raise ScriptError("FakeProvider script exhausted")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        if step.role is not Role.ASSISTANT:
            raise ScriptError("scripted messages must have the assistant role")
        for block in step.content:
            if isinstance(block, TextBlock):
                for i in range(0, len(block.text), self._chunk):
                    yield ProviderTextDelta(block.text[i : i + self._chunk])
        prompt_size = sum(len(m.model_dump_json()) for m in request.messages)
        yield ProviderMessage(
            message=step,
            usage=Usage(
                input_tokens=prompt_size // 4, output_tokens=len(step.model_dump_json()) // 4
            ),
        )
