"""LLM abstraction layer (TZ v4 §13 / §30).

Nothing else in the app should import a specific LLM SDK directly —
only this interface. Swapping providers means changing LLM_PROVIDER in
.env, not touching manager/agent code. No agent calls this yet (that's
Phase 7); Phase 2 only wires up the abstraction and a couple of tests.
"""
from __future__ import annotations

import abc
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from config import AppConfig

logger = logging.getLogger(__name__)


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
