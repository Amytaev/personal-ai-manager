"""LLM abstraction layer (TZ v4 §13 / §30).

Nothing else in the app should import a specific LLM SDK directly —
only this interface. Swapping providers means changing LLM_PROVIDER in
.env, not touching manager/agent code. No agent calls this yet (that's
Phase 7); Phase 2 only wires up the abstraction and a couple of tests.
"""
from __future__ import annotations

import abc
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)


@dataclass
class ToolCall:
    """One tool invocation the LLM asked for (AI Manager ТЗ v3 §10/§86).
    ``id`` is the provider's own call id (needed to correlate a tool's
    result back to this exact call in the next turn - Anthropic's tool-
    use protocol requires this); ``arguments`` is whatever JSON object
    the model supplied, UNVALIDATED - the Tool Registry (not this
    module) is what enforces each tool's real parameter schema (spec
    §16/§46 - the LLM proposing a call is not the same as that call
    being allowed to run)."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMToolResult:
    """One provider turn's outcome (spec §86 step 4-5): either the model
    is done and ``text`` is the final answer (``tool_calls`` empty), or
    it wants one or more tools run first (``tool_calls`` non-empty,
    ``text`` usually None) - AI Manager's tool-calling loop (agents to
    come) executes those, feeds the results back as the next turn, and
    calls generate_with_tools() again."""

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: The provider's own raw assistant-turn content (Anthropic's list of
    #: content blocks, for AnthropicProvider) - opaque to everything
    #: except this same provider. A caller doing a multi-turn tool loop
    #: (agents/ai_manager/agent.py) echoes this straight back as that
    #: turn's assistant message rather than trying to reconstruct it
    #: from ``text``/``tool_calls`` alone, since Anthropic's tool-use
    #: protocol requires the exact original tool_use blocks (including
    #: fields this dataclass doesn't otherwise preserve) to appear
    #: again before a matching tool_result. None for providers/turns
    #: that never propose a tool call in the first place.
    raw_content: Any = None


class LLMProvider(abc.ABC):
    @abc.abstractmethod
    async def generate(self, prompt: str) -> str:
        raise NotImplementedError

    async def summarize(self, data: dict) -> str:
        """Default summarize() builds a prompt and calls generate().
        A concrete provider can override this if it wants a different
        strategy (e.g. a dedicated summarization endpoint)."""
        prompt = (
            "Summarize the following structured data for a short, friendly "
            "Telegram briefing:\n\n" + repr(data)
        )
        return await self.generate(prompt)

    async def generate_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
    ) -> LLMToolResult:
        """AI Manager ТЗ v3 §6/§10's real tool-calling contract - a
        provider that supports native tool-use (AnthropicProvider below)
        overrides this. The DEFAULT implementation here deliberately
        does NOT attempt any tool-calling of its own (no fake JSON-in-
        prompt tool parsing, which would be exactly the kind of
        unvalidated "LLM invents a tool call" path spec §46/§88 warns
        against) - it just answers with plain generate() on the last
        user message and reports zero tool_calls, so a provider without
        real tool support (or NullProvider, when no LLM_API_KEY is
        configured at all) degrades to an honest plain-text reply
        instead of AI Manager crashing or hallucinating a tool call.
        """
        last_user_text = next(
            (m.get("content") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        prompt = last_user_text if not system else f"{system}\n\n{last_user_text}"
        text = await self.generate(prompt)
        return LLMToolResult(text=text, tool_calls=[])


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, model: str):
        self._api_key = api_key
        self._model = model

    async def generate(self, prompt: str) -> str:
        # Imported lazily so the dependency is optional when a different
        # LLM_PROVIDER is configured.
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=self._api_key)
        response = await client.messages.create(
            model=self._model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return "".join(block.text for block in response.content if block.type == "text")

    async def generate_with_tools(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        system: str | None = None,
    ) -> LLMToolResult:
        """Real tool-use via Anthropic's native Messages API tool-calling
        (not a prompt-engineered imitation) - ``tools`` is already in
        Anthropic's own {"name", "description", "input_schema"} shape
        (agents/ai_manager/tool_registry.py builds it that way once,
        provider-agnostically is NOT attempted here - see this
        project's Study Manager precedent for "don't build unnecessary
        abstraction ahead of a second real need", spec §7 says the same
        about not adding providers preemptively)."""
        import anthropic

        client = anthropic.AsyncAnthropic(api_key=self._api_key)
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": 1024,
            "messages": messages,
            "tools": tools,
        }
        if system:
            kwargs["system"] = system
        response = await client.messages.create(**kwargs)

        text_parts = [block.text for block in response.content if block.type == "text"]
        tool_calls = [
            ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
            for block in response.content
            if block.type == "tool_use"
        ]
        return LLMToolResult(
            text="".join(text_parts) or None,
            tool_calls=tool_calls,
            raw_content=response.content,
        )


class NullProvider(LLMProvider):
    """Used when no LLM_API_KEY is configured — keeps Phase 2 (and its
    tests) from requiring a real API key."""

    async def generate(self, prompt: str) -> str:
        logger.warning("NullProvider.generate() called - no LLM_API_KEY configured")
        return "[LLM not configured]"


def get_provider(config: "AppConfig") -> LLMProvider:
    if not config.llm_api_key:
        return NullProvider()
    if config.llm_provider == "anthropic":
        return AnthropicProvider(api_key=config.llm_api_key, model=config.llm_model)
    raise ValueError(f"Unsupported LLM_PROVIDER: {config.llm_provider!r}")
