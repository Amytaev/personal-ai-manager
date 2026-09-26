"""Entrypoint (Phase 5: + Teams Agent, on top of Phase 4's Core
Infrastructure + Telegram Bot + Weather Agent).

Wires together config, logging, the single-instance guard, the SQLite
database, the scheduler, the Telegram bot, the Weather Agent and now
the Teams Agent. VALORANT is still Phase 6, and the AI Manager that
would aggregate/prioritize/summarize everything is Phase 7 - /store
still says so honestly; /weather, /tasks and /briefing now show real
data once the relevant agent has run at least once.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal

from agents.runner import run_and_record
from agents.teams.agent import TeamsAgent
from agents.weather import WeatherAgent
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


async def _teams_cycle(agent: TeamsAgent, db: Database) -> None:
    # Unlike _weather_cycle, TeamsAgent.run() already writes its own rows
    # (storage/database.py's upsert_task) as part of run() itself, since
    # per-task new/changed/unchanged state has to be computed against
    # what's already stored - there's nothing left for run.py to persist
    # afterwards, just the agent_runs bookkeeping run_and_record() does.
    await run_and_record(agent, db)


async def _weather_cycle(agent: WeatherAgent, db: Database) -> None:
    result = await run_and_record(agent, db)
    if result.status.value == "working":
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO weather_snapshots (location, payload_json, created_at) "
                "VALUES (?, ?, ?)",
                (result.data.get("location", ""), json.dumps(result.data, ensure_ascii=False), result.timestamp),
            )


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
    weather_agent: WeatherAgent | None = None
    teams_agent: TeamsAgent | None = None

    # Everything from here on is inside one try/finally so that a failure
    # at ANY startup step (scheduler, the Telegram bot failing to reach
    # api.telegram.org, etc.) still releases the single-instance lock -
    # see the Phase 3 postmortem in this file's git history for why that
    # matters (a leaked lock file used to falsely block every later run).
    try:
        scheduler.register(
            job_id="retention_cleanup",
            interval_minutes=24 * 60,
            func=lambda: _daily_retention_cleanup(db, config.data_retention_days),
        )

        if config.weather_location:
            weather_agent = WeatherAgent(location=config.weather_location)
            scheduler.register(
                job_id="weather_check",
                interval_minutes=config.weather_interval_minutes,
                func=lambda: _weather_cycle(weather_agent, db),
            )
        else:
            logger.warning("WEATHER_LOCATION not set - Weather Agent will not run")

        # No on/off config flag for Teams (unlike weather's location/API
        # key) - the persistent browser profile is the gate instead: if
        # scripts/teams_login_setup.py hasn't been run locally yet, this
        # still starts fine and every cycle just reports FAILING (agent.py
        # catches its own auth/API errors), same honest degrade-not-crash
        # pattern as every other agent here.
        teams_agent = TeamsAgent(profile_dir=config.teams_profile_path, db=db)
        scheduler.register(
            job_id="teams_check",
            interval_minutes=config.teams_interval_minutes,
            func=lambda: _teams_cycle(teams_agent, db),
        )

        scheduler.start()

        if weather_agent is not None:
            # Run once immediately at startup, in addition to the
            # periodic job below - otherwise /weather would show nothing
            # for up to WEATHER_INTERVAL_MINUTES after every restart.
            # _weather_cycle already catches expected failures via
            # run_and_record/run_isolated, so this can't crash startup.
            await _weather_cycle(weather_agent, db)

        if teams_agent is not None:
            # Same immediate-first-run rationale as weather above - and
            # since this may open a real (headless) browser against a
            # profile that was never logged in (e.g. first-ever run),
            # _teams_cycle/run_and_record already turn that into a
            # FAILING AgentResult rather than letting it crash startup.
            await _teams_cycle(teams_agent, db)

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
        # startup failure above.
        #
        # Each cleanup step is wrapped individually so that one failing
        # can never prevent guard.release() from running - a leaked lock
        # file is worse than a partially clean shutdown.
        if bot is not None:
            try:
                await bot.stop()
            except Exception:  # noqa: BLE001 - best-effort cleanup boundary
                logger.exception("Error while stopping the Telegram bot")
        if weather_agent is not None:
            try:
                await weather_agent.aclose()
            except Exception:  # noqa: BLE001 - best-effort cleanup boundary
                logger.exception("Error while closing the Weather Agent's HTTP client")
        if teams_agent is not None:
            try:
                await teams_agent.aclose()
            except Exception:  # noqa: BLE001 - best-effort cleanup boundary
                logger.exception("Error while closing the Teams Agent's browser session")
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
