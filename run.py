"""Entrypoint (Phase 3: Core Infrastructure + Telegram Bot).

Wires together config, logging, the single-instance guard, the SQLite
database, the scheduler and now the Telegram bot. Teams, Weather and
VALORANT agents are still Phase 4/5/6, and the AI Manager that would
aggregate/prioritize/summarize them is Phase 7 - /tasks, /weather,
/store and /briefing read straight from the (currently empty) SQLite
tables and say so honestly until those phases land.
"""
from __future__ import annotations

import asyncio
import logging
import signal

from config import load_config
from logging_config import setup_logging
from scheduler.scheduler import Scheduler, SingleInstanceError, SingleInstanceGuard
from storage.database import Database
from storage.wishlist import WishlistStore
from telegram_bot.bot import TelegramBot

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
    wishlist = WishlistStore(config.wishlist_path)
    scheduler = Scheduler()
    bot: TelegramBot | None = None

    # Everything from here on is inside one try/finally so that a failure
    # at ANY startup step (scheduler, or - discovered while testing this -
    # the Telegram bot failing to reach api.telegram.org, e.g. no network
    # or a bad token) still releases the single-instance lock. Without
    # this, a crash during bot.start() left the lock file behind forever,
    # so every later run falsely reported "Application already running"
    # even though nothing was actually running.
    try:
        scheduler.register(
            job_id="retention_cleanup",
            interval_minutes=24 * 60,
            func=lambda: _daily_retention_cleanup(db, config.data_retention_days),
        )
        scheduler.start()

        if config.telegram_bot_token and config.telegram_chat_id:
            bot = TelegramBot(config=config, db=db, wishlist=wishlist)
            await bot.start()
        else:
            logger.warning(
                "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set - running without the Telegram bot"
            )

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()

        def _handle_stop_signal() -> None:
            logger.info("Shutdown signal received")
            stop_event.set()

        # asyncio.loop.add_signal_handler() is NOT implemented on Windows
        # at all (raises NotImplementedError unconditionally on the
        # default ProactorEventLoop) - and Windows 11 is this project's
        # target platform (TZ v4 §2.1). So SIGTERM handling below is a
        # best-effort extra for Linux/Mac dev machines only; the portable
        # path that also works on Windows is the bare `finally` below,
        # since Ctrl+C (SIGINT) reliably cancels this task on every
        # platform (see the note in the finally block).
        try:
            loop.add_signal_handler(signal.SIGTERM, _handle_stop_signal)
        except (NotImplementedError, RuntimeError, AttributeError):
            pass

        logger.info("Personal AI Manager is running. Ctrl+C to stop.")
        await stop_event.wait()
    finally:
        # A bare `finally` (not `except KeyboardInterrupt`) is required:
        # asyncio.run() handles a bare Ctrl+C by cancelling this task
        # (raising asyncio.CancelledError right here, not
        # KeyboardInterrupt) and only re-raises KeyboardInterrupt itself
        # *after* run_until_complete() returns - by which point we're no
        # longer inside this coroutine. `finally` runs on any exception,
        # so cleanup happens on the graceful SIGTERM/SIGINT path
        # (Linux/Mac), the Ctrl+C/CancelledError path (Windows), AND a
        # startup failure above (e.g. bot.start() raising).
        #
        # Each cleanup step is wrapped individually so that one failing
        # (e.g. scheduler.shutdown() raising because it was never
        # successfully started) can never prevent guard.release() from
        # running - a leaked lock file is worse than a partially clean
        # shutdown.
        if bot is not None:
            try:
                await bot.stop()
            except Exception:  # noqa: BLE001 - best-effort cleanup boundary
                logger.exception("Error while stopping the Telegram bot")
        try:
            await scheduler.shutdown(wait=True)
        except Exception:  # noqa: BLE001 - best-effort cleanup boundary
            logger.exception("Error while stopping the scheduler")
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
