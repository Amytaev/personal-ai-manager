"""Tool Registry core types (AI Manager ТЗ v3 §10/§16/§27/§46).

"LLM не вызывает Agents напрямую. LLM вызывает зарегистрированный
tool." - this module is the contract that makes that literally true:
a Tool is a name, a description, a strict JSON-Schema-shaped parameter
allowlist, and a handler. ToolRegistry.execute() is the ONE place a
tool call from the LLM is actually run, and it enforces the allowlist
BEFORE the handler ever sees the arguments - an LLM proposing
`{"field": "some_internal_database_column"}` for a tool whose schema
doesn't list that key is rejected right here, never passed through
(spec §16/§46's "Tool Registry, not the LLM, decides what's allowed").

Nothing here imports Telegram, an LLM SDK, or a concrete Agent -
tools/study.py/tools/weather.py/tools/valorant.py wire real handlers
in; this module only defines the shape.
"""
from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)


@dataclass
class ToolResult:
    """spec §27's exact wire shape - every tool call ends as one of
    these, whether it succeeded, was rejected by the allowlist, or the
    handler itself raised."""

    status: str  # "OK" | "ERROR"
    data: Any = None
    error: dict[str, str] | None = None

    @classmethod
    def ok(cls, data: Any = None) -> "ToolResult":
        return cls(status="OK", data=data, error=None)

    @classmethod
    def fail(cls, code: str, message: str) -> "ToolResult":
        return cls(status="ERROR", data=None, error={"code": code, "message": message})

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "data": self.data, "error": self.error}


@dataclass
class Tool:
    """One registered tool. ``parameters`` is a JSON-Schema object
    ({"type": "object", "properties": {...}, "required": [...]}) - the
    single source of truth for both what ToolRegistry.execute() will
    accept from the LLM (spec §16/§46) AND what's sent to the provider
    as this tool's ``input_schema`` (spec §57's "each tool must have a
    precise description"). ``handler`` receives only the validated
    keyword arguments (never the raw, unchecked dict from the LLM) and
    returns plain JSON-serializable data - never a live Python object
    (spec §86's "not give the LLM direct access to internal Python
    objects"). ``read_only=False`` marks a LOCAL WRITE tool (spec §48 -
    e.g. set_task_override) for the AI Manager layer's own logging/
    confirmation-policy decisions; ToolRegistry itself doesn't treat
    the two differently beyond that flag being available to read."""

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Any] | Callable[..., Awaitable[Any]]
    read_only: bool = True

    def to_anthropic_schema(self) -> dict[str, Any]:
        """The exact {"name", "description", "input_schema"} shape
        Anthropic's tool-use API expects (llm/provider.py's
        AnthropicProvider.generate_with_tools() passes this list
        straight through as ``tools``)."""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }


class ToolValidationError(ValueError):
    """Raised by ToolRegistry.execute()'s own allowlist check - never by
    a handler itself (a handler's own validation errors, e.g. agents/
    study_manager/logic.py's OverrideValidationError, are caught
    separately and translated into their own ToolResult.fail())."""


class ToolRegistry:
    """Central list of every tool AI Manager may call (spec §10). Holds
    no state about WHICH agents/services back each tool - tools/study.py
    /tools/weather.py/tools/valorant.py build the concrete Tool objects
    (each one closed over its own StudyManager/db, so this registry
    itself never imports an Agent directly)."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name!r}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def anthropic_tools(self) -> list[dict[str, Any]]:
        """The full tool list in the shape generate_with_tools() expects
        - what the LLM actually sees (spec §57 - each with its own
        precise description, never a vague catch-all)."""
        return [self._tools[name].to_anthropic_schema() for name in self.names()]

    def _validate_arguments(self, tool: Tool, arguments: dict[str, Any]) -> dict[str, Any]:
        """spec §16/§46's allowlist gate: every key in ``arguments`` MUST
        appear in ``tool.parameters["properties"]`` - anything else is
        rejected outright, never silently dropped-and-ignored (that
        would hide a bug/an injection attempt equally badly) nor passed
        through to the handler. Every key in ``required`` MUST be
        present. No type coercion happens here - a handler that needs a
        specific type still validates it itself (see e.g. agents/
        study_manager/logic.py's validate_override_field(), which this
        allowlist check deliberately does not duplicate)."""
        properties = tool.parameters.get("properties", {})
        required = tool.parameters.get("required", [])

        unknown = set(arguments) - set(properties)
        if unknown:
            raise ToolValidationError(f"unknown argument(s) for {tool.name}: {sorted(unknown)}")

        missing = [key for key in required if key not in arguments]
        if missing:
            raise ToolValidationError(f"missing required argument(s) for {tool.name}: {missing}")

        return {key: arguments[key] for key in properties if key in arguments}

    async def execute(self, name: str, arguments: dict[str, Any] | None = None) -> ToolResult:
        """The ONE place a tool call actually runs (spec §86 step 4-5).
        Always returns a ToolResult, never raises - a handler exception,
        a validation failure, or an unknown tool name are all reported
        back as ToolResult.fail(), the same "never let one tool crash
        the caller" discipline as agents/base.py's run_isolated()."""
        arguments = arguments or {}
        tool = self.get(name)
        if tool is None:
            return ToolResult.fail("UNKNOWN_TOOL", f"no such tool: {name!r}")

        try:
            validated = self._validate_arguments(tool, arguments)
        except ToolValidationError as exc:
            logger.warning("Tool Registry: rejected call to %s - %s", name, exc)
            return ToolResult.fail("INVALID_ARGUMENTS", str(exc))

        try:
            result = tool.handler(**validated)
            if inspect.isawaitable(result):
                result = await result
        except Exception as exc:  # noqa: BLE001 - per-tool isolation boundary (spec §85)
            # Exception type + message only, never a traceback and
            # never raw request/response data - same discipline as
            # every other error-boundary in this project (agents/base.py's
            # run_isolated(), agents/study_manager/agent.py's _run_step()).
            logger.warning("Tool Registry: %s raised %s: %s", name, type(exc).__name__, exc)
            return ToolResult.fail(type(exc).__name__, str(exc))

        return ToolResult.ok(result)
