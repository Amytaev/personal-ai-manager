"""Run-and-record cycle for scheduled agents (Phase 4+).

Wraps run_isolated() (agents/base.py) with agent_runs bookkeeping, so
/status and last_run() have real data to show. This is intentionally
NOT the AI Manager - there's no aggregation, prioritization or
notification logic here, just "run it safely, log what happened".
Phase 7 will likely absorb this into the real Manager.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from agents.base import AgentResult, AgentStatus, BaseAgent, run_isolated
from storage.database import Database

logger = logging.getLogger(__name__)


async def run_and_record(agent: BaseAgent, db: Database) -> AgentResult:
    started_at = datetime.now(timezone.utc).isoformat()
    result = await run_isolated(agent)
    finished_at = datetime.now(timezone.utc).isoformat()

    db.record_agent_run(
        agent=result.agent,
        status=result.status.value,
        started_at=started_at,
        finished_at=finished_at,
        error=result.error,
    )

    if result.status != AgentStatus.WORKING:
        logger.warning("%s agent run ended in %s: %s", result.agent, result.status.value, result.error)
    else:
        logger.info("%s agent run OK", result.agent)

    return result
