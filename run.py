"""Entrypoint (Phase 2: Core Infrastructure only).

Wires together config, logging, the single-instance guard, the SQLite
database and the scheduler. No agents are registered yet — Teams,
Weather and VALORANT are Phase 4/5/6, and the AI Manager that would
register them is Phase 7. Running this script proves the foundation
works end-to-end (starts, holds the lock, runs the daily retention
cleanup job, shuts down cleanly) without doing anything else yet.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from config import load_config
from logging_config import setup_logging
from scheduler.scheduler import Scheduler, SingleInstanceError, SingleInstanceGuard
from storage.database import Database

logger = logging.getLogger(__name__)


async def _daily_retention_cleanup(db: Database, retention_days: int) -> None:
    deleted = db.cleanup(retention_days)
    logger.info("Retention cleanup: %s", deleted)


async def main() -> None:
    config = load_config()
    setup_logging(config.log_dir)

    guard = SingleInstanceGuard(config.lock_file_path)
    try:
        guard.acquire()
    except SingleInstanceError as exc:
        logger.error(str(exc))
        return

    db = Database(config.database_path)
    scheduler = Scheduler()

    # Daily retention cleanup (TZ v4 §12) - the only job Phase 2 has
    # anything to schedule, since no agents exist yet.
    scheduler.register(
        job_id="retention_cleanup",
        interval_minutes=24 * 60,
        func=lambda: _daily_retention_cleanup(db, config.data_retention_days),
    )
    scheduler.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle_stop_signal() -> None:
        logger.info("Shutdown signal received")
        stop_event.set()

    # asyncio.loop.add_signal_handler() is NOT implemented on Windows at
    # all (raises NotImplementedError unconditionally on the default
    # ProactorEventLoop) - and Windows 11 is this project's target
    # platform (TZ v4 §2.1). So SIGTERM handling below is a best-effort
    # extra for Linux/Mac dev machines only; the portable path that also
    # works on Windows is catching KeyboardInterrupt around the wait
    # below, since Ctrl+C (SIGINT) reliably raises it on every platform.
    try:
        loop.add_signal_handler(signal.SIGTERM, _handle_stop_signal)
    except (NotImplementedError, RuntimeError, AttributeError):
        pass

    logger.info("Personal AI Manager (Phase 2 core) is running. Ctrl+C to stop.")
    try:
        await stop_event.wait()
    finally:
        # A bare `finally` (not `except KeyboardInterrupt`) is required:
        # asyncio.run() handles a bare Ctrl+C by cancelling this task
        # (raising asyncio.CancelledError right here, not
        # KeyboardInterrupt) and only re-raises KeyboardInterrupt itself
        # *after* run_until_complete() returns - by which point we're no
        # longer inside this coroutine. `finally` runs on any exception,
        # so cleanup happens on both the graceful SIGTERM/SIGINT path
        # (Linux/Mac, via add_signal_handler above) and the Ctrl+C/
        # CancelledError path (Windows, where add_signal_handler isn't
        # available at all).
        await scheduler.shutdown(wait=True)
        guard.release()
        logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Reaches here only on the Windows/no-signal-handler path,
        # after main()'s finally block has already released the lock
        # and logged "Shutdown complete" - this just avoids printing a
        # scary traceback for a normal Ctrl+C stop.
        pass
