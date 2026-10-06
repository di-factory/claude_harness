"""Tools behind the governance plane.

Arguments reach a tool with only the PII classes ``reveal_to_tools`` grants it (other
tokens stay tokens); results are tokenized before the model sees them. The registry
interface is unchanged, so the loop and the permission gate never know.

Knowledge passages are the business's own published documents: their tokens (its email,
phone, address) are marked published, so the reply shows the values while the model only
ever sees tokens.
"""

from __future__ import annotations

from ..core.messages import ToolResultBlock, ToolUseBlock
from ..tools.registry import ToolRegistry
from .pii import Tokenizer

PUBLISHED_NOTE = (
    "Tokens such as <EMAIL_...> or <PHONE_...> in these passages are the business's own"
    " published details: write them exactly as they are when they answer the question; the"
    " contact sees the real values."
)


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
        content = await self.pii.tokenize_obj(result.content)
        tool = self.inner.get(call.name)
        published = tool is not None and tool.source == "knowledge" and self.pii.publish(content)
        if published and isinstance(content, dict):
            content["published_details"] = PUBLISHED_NOTE
        return result.model_copy(
            update={
                "content": content,
                "error": await self.pii.tokenize(result.error) if result.error else None,
            }
        )
