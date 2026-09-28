"""AIManager (AI Manager ТЗ v3 §86) - the tool-calling loop that turns a
natural-language message into a final answer:

1. intent detection - delegated to the LLM's own tool-calling (never a
   hand-rolled keyword router here - the LLM decides which tool(s), if
   any, this message needs).
2. tool selection - the LLM proposes zero or more tool_calls.
3. parameter validation - tools/base.py's ToolRegistry allowlist gate,
   BEFORE any handler runs (spec §16/§46).
4. tool execution - ToolRegistry.execute(), one call at a time, each
   wrapped in its own timeout (spec §63) so one hung tool can't hang
   the whole turn.
5. result aggregation - every tool result is fed back to the model as
   the next turn (spec §35: only the relevant tool result, never the
   whole database).
6. response generation - looped until the model stops proposing tools
   and returns final text, or agent iterations run out.

AIManager itself never touches SQLite, a browser, an LLM SDK, or
Telegram directly (spec §5/§43) - only llm.provider.LLMProvider and
tools.base.ToolRegistry, both handed in by whoever constructs this
(run.py / a future Telegram adapter), never constructed here.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable

from agents.ai_manager.prompt import build_system_prompt
from llm.provider import LLMProvider
from tools.base import ToolRegistry, ToolResult

logger = logging.getLogger(__name__)

#: spec §86/§2 - LLM decides tool calls, not an unbounded agent loop.
#: Six round-trips is generous for even the most involved multi-tool
#: request (spec §31's dashboard/schedule/weather example needs two);
#: this exists purely as a circuit breaker against a misbehaving model
#: that keeps calling tools forever without ever answering.
DEFAULT_MAX_TOOL_ITERATIONS = 6

#: spec §63 - one tool call must never hang a whole request. Generous
#: for refresh_study_data (a real Teams/SSO sync, spec's own "may take
#: longer than read operations" note), short enough that a genuinely
#: stuck browser session still surfaces as an honest TOOL_TIMEOUT
#: rather than silence.
DEFAULT_TOOL_TIMEOUT_SECONDS = 60.0

#: spec §35 - never forward an absurdly large tool result to the model
#: unfiltered; a generous ceiling that only ever catches a genuinely
#: oversized/broken result, not an ordinary dashboard payload.
MAX_TOOL_RESULT_CHARS = 20_000


@dataclass
class ToolCallLog:
    """spec §61's per-tool-call observability record - name/status/
    duration only, never the raw arguments or result content (those can
    legitimately contain a user's own study data, which doesn't belong
    in a log line any more than a password would - see spec §61's own
    "не логировать ... полный чувствительный LLM context")."""

    name: str
    status: str
    duration_ms: float


@dataclass
class AIManagerResult:
    request_id: str
    text: str
    tool_calls: list[ToolCallLog] = field(default_factory=list)
    #: "OK" | "LLM_PROVIDER_ERROR" | "TOOL_TIMEOUT" | "MAX_ITERATIONS" -
    #: spec §28's status vocabulary, reused at the AI Manager layer for
    #: its own outcome (never conflated with a source's own
    #: OK/AUTH_REQUIRED/... status, which lives inside tool results).
    status: str = "OK"


class LLMProviderError(Exception):
    """spec §89 - the LLM provider itself failed (network, auth, rate
    limit). Never propagated raw to the caller - handle_message() turns
    this into an honest, non-technical AIManagerResult instead; the
    real exception text stays in the log only (spec §89's "не
    раскрывать внутренний stack trace")."""


def _truncate_for_model(text: str) -> str:
    if len(text) <= MAX_TOOL_RESULT_CHARS:
        return text
    return text[:MAX_TOOL_RESULT_CHARS] + "...(truncated)"


class AIManager:
    def __init__(
        self,
        provider: LLMProvider,
        tool_registry: ToolRegistry,
        *,
        system_prompt_builder: Callable[[], str] | None = None,
        max_tool_iterations: int = DEFAULT_MAX_TOOL_ITERATIONS,
        tool_timeout_seconds: float = DEFAULT_TOOL_TIMEOUT_SECONDS,
        provider_retries: int = 1,
    ):
        self.provider = provider
        self.tool_registry = tool_registry
        # A callable, not a plain string - spec §52 needs "now" embedded
        # fresh on every turn (a session that runs for hours must not
        # keep telling the model yesterday's date), so this is called
        # once per handle_message(), never cached across calls.
        self.system_prompt_builder = system_prompt_builder or build_system_prompt
        self.max_tool_iterations = max_tool_iterations
        self.tool_timeout_seconds = tool_timeout_seconds
        # spec §64 - LIMITED retry, only for the provider call itself,
        # and only for what the provider raises as a transient failure
        # (a network hiccup, a 5xx). Never retried: a tool call itself
        # (ToolRegistry.execute() already never raises - a tool that
        # failed becomes an ERROR ToolResult the model sees and can act
        # on, not something to blindly re-run) and never a browser
        # action inside a tool (spec §64's explicit "retry браузерных
        # операций остаётся ответственностью соответствующего Agent").
        self.provider_retries = provider_retries

    async def handle_message(
        self, user_text: str, history: list[dict[str, Any]] | None = None
    ) -> AIManagerResult:
        """spec §62 - one ``request_id`` per call, threading through
        every tool-call log line for this turn so a Telegram message,
        its tool calls, and its final response can all be correlated
        later without needing to log any of their actual content."""
        request_id = uuid.uuid4().hex
        messages: list[dict[str, Any]] = list(history or [])
        messages.append({"role": "user", "content": user_text})
        system_prompt = self.system_prompt_builder()
        tools = self.tool_registry.anthropic_tools()
        tool_log: list[ToolCallLog] = []

        for _iteration in range(self.max_tool_iterations):
            try:
                turn = await self._generate_with_retry(messages, tools, system_prompt)
            except LLMProviderError as exc:
                logger.warning("AI Manager [%s]: LLM provider failed - %s", request_id, exc)
                return AIManagerResult(
                    request_id=request_id,
                    text="Не получилось обратиться к LLM прямо сейчас. Попробуй ещё раз чуть позже.",
                    tool_calls=tool_log,
                    status="LLM_PROVIDER_ERROR",
                )

            if not turn.tool_calls:
                return AIManagerResult(
                    request_id=request_id, text=turn.text or "", tool_calls=tool_log, status="OK"
                )

            # Echo the assistant's own turn back exactly as the provider
            # gave it (see llm/provider.py's LLMToolResult.raw_content
            # docstring) - required so the NEXT turn's tool_result
            # blocks correctly match up with these tool_use blocks.
            messages.append({
                "role": "assistant",
                "content": turn.raw_content if turn.raw_content is not None else (turn.text or ""),
            })

            tool_result_blocks = []
            for call in turn.tool_calls:
                started = time.monotonic()
                try:
                    result = await asyncio.wait_for(
                        self.tool_registry.execute(call.name, call.arguments),
                        timeout=self.tool_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    result = ToolResult.fail(
                        "TOOL_TIMEOUT", f"{call.name} did not finish within {self.tool_timeout_seconds:.0f}s"
                    )
                duration_ms = (time.monotonic() - started) * 1000
                tool_log.append(ToolCallLog(name=call.name, status=result.status, duration_ms=duration_ms))
                logger.info(
                    "AI Manager [%s]: tool=%s status=%s duration_ms=%.0f",
                    request_id, call.name, result.status, duration_ms,
                )
                tool_result_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": call.id,
                    "content": _truncate_for_model(json.dumps(result.to_dict(), ensure_ascii=False)),
                })

            messages.append({"role": "user", "content": tool_result_blocks})

        logger.warning(
            "AI Manager [%s]: exceeded max_tool_iterations=%s without a final answer",
            request_id, self.max_tool_iterations,
        )
        return AIManagerResult(
            request_id=request_id,
            text="Не смог довести запрос до ответа за разумное число шагов — попробуй переформулировать.",
            tool_calls=tool_log,
            status="MAX_ITERATIONS",
        )

    async def _generate_with_retry(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]], system_prompt: str
    ):
        last_exc: Exception | None = None
        for attempt in range(self.provider_retries + 1):
            try:
                return await self.provider.generate_with_tools(messages, tools, system=system_prompt)
            except Exception as exc:  # noqa: BLE001 - provider error boundary, spec §85/§89
                last_exc = exc
                if attempt < self.provider_retries:
                    logger.info(
                        "AI Manager: LLM provider call failed, retrying (%s/%s): %s: %s",
                        attempt + 1, self.provider_retries, type(exc).__name__, exc,
                    )
                    continue
        raise LLMProviderError(f"{type(last_exc).__name__}: {last_exc}") from last_exc
