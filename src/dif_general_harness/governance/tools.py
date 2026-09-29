"""Tools behind the governance plane.

Arguments reach a tool with only the PII classes ``reveal_to_tools`` grants it (other
tokens stay tokens); results are tokenized before the model sees them. The registry
interface is unchanged, so the loop and the permission gate never know.
"""

from __future__ import annotations

from ..core.messages import ToolResultBlock, ToolUseBlock
from ..tools.registry import ToolRegistry
from .pii import Tokenizer


class GovernedTools(ToolRegistry):
    def __init__(self, inner: ToolRegistry, pii: Tokenizer) -> None:
        super().__init__()
        self._tools = inner._tools
        self.inner = inner
        self.pii = pii

    async def execute(self, call: ToolUseBlock) -> ToolResultBlock:
        if not self.pii.active:
            return await self.inner.execute(call)
        reveal = self.pii.policy.for_tool(call.name)
        args = await self.pii.detokenize_obj(call.input, reveal) if reveal else call.input
        result = await self.inner.execute(call.model_copy(update={"input": args}))
        return result.model_copy(
            update={
                "content": await self.pii.tokenize_obj(result.content),
                "error": await self.pii.tokenize(result.error) if result.error else None,
            }
        )
