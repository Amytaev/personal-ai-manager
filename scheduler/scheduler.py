"""Scheduler foundation (TZ v4 §8, §36 Single Instance, §37 Graceful Shutdown).

This module provides only the *foundation*: registering jobs at
configurable per-agent intervals, a single-instance guard, and graceful
shutdown. Wiring actual agents into it happens in Phase 7 (AI Manager)
once the agents themselves exist (Phase 4/5/6).
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger(__name__)


class SingleInstanceError(RuntimeError):
    """Raised when another copy of the app already holds the lock file."""


class SingleInstanceGuard:
    """A simple file lock: prevents a second copy of the app from
    starting a second scheduler and duplicating Telegram notifications
    (TZ v4 §36). Not a distributed lock — that's more than this
    single-machine project needs.
    """

    def __init__(self, lock_path: str | Path):
        self.lock_path = Path(lock_path)
        self._fd: int | None = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            # O_EXCL: fails if the file already exists -> another instance holds it.
            self._fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            os.write(self._fd, str(os.getpid()).encode())
        except FileExistsError as exc:
            raise SingleInstanceError(
                f"Application already running (lock file: {self.lock_path})"
            ) from exc

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.lock_path.unlink(missing_ok=True)

    def __enter__(self) -> "SingleInstanceGuard":
        self.acquire()
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


class Scheduler:
    """Thin wrapper around APScheduler with per-job interval config."""

    def __init__(self) -> None:
        self._scheduler = AsyncIOScheduler()

    def register(
        self,
        job_id: str,
        interval_minutes: int,
        func: Callable[[], Awaitable[None]],
    ) -> None:
        self._scheduler.add_job(
            func,
            trigger="interval",
            minutes=interval_minutes,
            id=job_id,
            replace_existing=True,
            max_instances=1,  # never run two cycles of the same job concurrently
        )
        logger.info("Registered job %s every %s minutes", job_id, interval_minutes)

    def start(self) -> None:
        self._scheduler.start()
        logger.info("Scheduler started")

    async def shutdown(self, wait: bool = True) -> None:
        """Graceful shutdown (TZ v4 §37): let in-flight jobs finish
        before returning, so we never kill a job mid-write to SQLite."""
        logger.info("Shutting down scheduler (wait=%s)", wait)
        self._scheduler.shutdown(wait=wait)
