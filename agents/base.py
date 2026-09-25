"""Base agent interface and structured result types (TZ v4 §4).

Every concrete agent (Teams, Weather, VALORANT, ...) implements BaseAgent.
The manager/scheduler only ever talk to agents through this contract, so
a single misbehaving agent can never take down the rest of the system.

No concrete agents live here yet — Teams/Weather/VALORANT are later
phases (5, 4, 6). This module is the contract they will all follow.
"""
from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


class AgentStatus(str, enum.Enum):
    WORKING = "working"
    DEGRADED = "degraded"
    FAILING = "failing"


@dataclass
class AgentResult:
    """The structured result every agent run produces (TZ v4 §4)."""

    agent: str
    status: AgentStatus
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "status": self.status.value,
            "timestamp": self.timestamp,
            "data": self.data,
            "error": self.error,
        }


class BaseAgent(abc.ABC):
    """Contract every agent must follow."""

    def __init__(self, name: str) -> None:
        self.name = name

    @abc.abstractmethod
    async def run(self) -> AgentResult:
        """Perform one check cycle and return a structured result.

        Implementations should not raise for *expected* failure modes
        (API down, auth expired, etc.) — catch those and return a
        DEGRADED/FAILING AgentResult instead. Unexpected exceptions are
        still caught by :func:`run_isolated` below, so a bug in one
        agent never crashes the scheduler loop (TZ v4 §10/§21).
        """
        raise NotImplementedError


async def run_isolated(agent: BaseAgent) -> AgentResult:
    """Run an agent and guarantee a structured result, even on crash.

    This is the error-isolation boundary required by the spec: whatever
    happens inside ``agent.run()``, the caller always gets an
    AgentResult back, never a raised exception. The manager/scheduler
    (Phase 7) will call every agent through this, not through
    ``agent.run()`` directly.
    """
    try:
        return await agent.run()
    except Exception as exc:  # noqa: BLE001 - intentional catch-all boundary
        # Deliberately not including a traceback here: exception messages
        # from third-party libraries can occasionally echo back request
        # data. Keep it to the exception type + message.
        return AgentResult(
            agent=agent.name,
            status=AgentStatus.FAILING,
            error=f"{type(exc).__name__}: {exc}",
        )
