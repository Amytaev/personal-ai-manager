"""Entrypoint (+ Auth Checker Agent on top of the SSO Agent (Этап B),
Phase 6's VALORANT Agent, Phase 5's Teams Agent, Phase 4's Core
Infrastructure + Telegram Bot + Weather Agent).

Wires together config, logging, the single-instance guard, the SQLite
database, the scheduler, the Telegram bot, and the Weather/Teams/
VALORANT/SSO/Auth Checker agents. The AI Manager that would aggregate/
prioritize/summarize everything is still Phase 7 - /weather, /tasks,
/store and /briefing now all show real data once the relevant agent has
run at least once. SSO Agent data (normalized schedule/УМКД) has no bot
command of its own yet - that's Study Manager/Mini App territory,
explicitly out of scope for this stage. Auth Checker only maintains
storage/models.py's source_status table (agents/auth_checker.py) for a
future Study Manager/health report to read - it has no bot command of
its own either.
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal

from agents.auth_checker import AuthCheckerAgent
from agents.runner import run_and_record
from agents.sso.agent import SsoAgent
from agents.sso.auth import SsoSession
from agents.teams.agent import TeamsAgent
from agents.teams.auth import TeamsSession
from agents.valorant.agent import ValorantAgent
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


async def _notify_needs_reauth(bot: TelegramBot | None, dedupe_key: str, text: str) -> None:
    # Shared by _teams_cycle and _valorant_cycle below - a stale browser
    # session needs a DISTINCT Telegram notification ("go log back in on
    # your PC"), not just a silent red marker in /status, since the
    # person may be away from their PC with no way to fix it themselves
    # until they notice. `bot` may still be None here (this can run once
    # before the bot is constructed, at startup - see main() below) -
    # skip the notification rather than crash the cycle either way,
    # run_and_record() already recorded the failure in agent_runs
    # regardless. Deduplicated via bot.notify()'s usual (kind,
    # dedupe_key) mechanism, so a still-broken session doesn't re-notify
    # every single cycle.
    if bot is not None:
        await bot.notify(kind="agent_status_change", dedupe_key=dedupe_key, text=text)


async def _teams_cycle(agent: TeamsAgent, db: Database, bot: TelegramBot | None) -> None:
    # Unlike _weather_cycle, TeamsAgent.run() already writes its own rows
    # (storage/database.py's upsert_task) as part of run() itself, since
    # per-task new/changed/unchanged state has to be computed against
    # what's already stored - there's nothing left for run.py to persist
    # afterwards, just the agent_runs bookkeeping run_and_record() does.
    result = await run_and_record(agent, db)

    if result.data.get("needs_reauth"):
        await _notify_needs_reauth(
            bot,
            dedupe_key="teams:needs_reauth",
            text=(
                "\U0001f4da Teams Agent: сессия Microsoft истекла.\n"
                "Открой приложение на ПК и войди в Teams заново:\n"
                "python -m scripts.teams_login_setup"
            ),
        )


async def _weather_cycle(agent: WeatherAgent, db: Database) -> None:
    result = await run_and_record(agent, db)
    if result.status.value == "working":
        with db.connect() as conn:
            conn.execute(
                "INSERT INTO weather_snapshots (location, payload_json, created_at) "
                "VALUES (?, ?, ?)",
                (result.data.get("location", ""), json.dumps(result.data, ensure_ascii=False), result.timestamp),
            )


async def _valorant_cycle(agent: ValorantAgent, db: Database, bot: TelegramBot | None) -> None:
    # Unlike _weather_cycle, ValorantAgent.run() already writes its own
    # row (storage/database.py's save_valorant_store) as part of run()
    # itself, same reasoning as _teams_cycle above - nothing left to
    # persist here.
    result = await run_and_record(agent, db)

    if result.data.get("needs_reauth"):
        await _notify_needs_reauth(
            bot,
            dedupe_key="valorant:needs_reauth",
            text=(
                "\U0001f3ae VALORANT Agent: сессия Stack B истекла.\n"
                "Открой приложение на ПК и войди в Stack B заново:\n"
                "python -m scripts.valorant_login_setup"
            ),
        )


async def _auth_checker_cycle(agent: AuthCheckerAgent, db: Database) -> None:
    # No Telegram notification here on purpose - AUTH_REQUIRED/ERROR for
    # a source is already surfaced by that source's own _teams_cycle/
    # _sso_cycle needs_reauth notification above, the first time its OWN
    # sync actually hits the problem. Auth Checker's job (bro's ТЗ) is
    # only to keep source_status current for a future health/status
    # report - duplicating the same alert from a second place would just
    # be redundant noise on the same underlying event.
    await run_and_record(agent, db)


async def _sso_cycle(agent: SsoAgent, db: Database, bot: TelegramBot | None) -> None:
    # Unlike _weather_cycle, SsoAgent.run() already writes its own rows
    # (storage/database.py's save_sso_snapshot) as part of run() itself,
    # same reasoning as _teams_cycle/_valorant_cycle above - nothing
    # left to persist here.
    result = await run_and_record(agent, db)

    if result.data.get("needs_reauth"):
        await _notify_needs_reauth(
            bot,
            dedupe_key="sso:needs_reauth",
            text=(
                "\U0001f393 SSO Agent: сессия sso.satbayev.university истекла.\n"
                "Открой приложение на ПК и войди заново:\n"
                "python -m scripts.sso_login_setup"
            ),
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
    valorant_agent: ValorantAgent | None = None
    sso_agent: SsoAgent | None = None
    auth_checker_agent: AuthCheckerAgent | None = None
    # Constructed here (not left for TeamsAgent/SsoAgent's own
    # session-or-None default) SPECIFICALLY so the same instances can
    # also be handed to AuthCheckerAgent below - both TeamsSession and
    # SsoSession are documented as "only one Chromium process may use a
    # given profile_dir at a time" (agents/teams/auth.py, agents/sso/
    # auth.py), so a second, independent session on the same profile_dir
    # would fight the real agent for that persistent profile's lock.
    # run.py owns these now (teams_agent/sso_agent's own _owns_session
    # becomes False), so they're closed in the finally block below
    # instead of by teams_agent.aclose()/sso_agent.aclose().
    teams_session = TeamsSession(config.teams_profile_path)
    sso_session = SsoSession(config.sso_profile_path)

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
        teams_agent = TeamsAgent(
            profile_dir=config.teams_profile_path,
            db=db,
            session=teams_session,
            min_due_date=config.teams_min_due_date,
        )
        scheduler.register(
            job_id="teams_check",
            interval_minutes=config.teams_interval_minutes,
            func=lambda: _teams_cycle(teams_agent, db, bot),
        )

        # No on/off config flag for VALORANT either, same reasoning as
        # Teams above - the persistent Stack B profile is the gate: if
        # scripts/valorant_login_setup.py hasn't been run locally yet,
        # this still starts fine and every cycle reports FAILING with
        # needs_reauth (agents/valorant/agent.py catches its own auth
        # errors). `bot` is read from this enclosing scope at CALL time,
        # not at registration time - it's still None here but will hold
        # the real TelegramBot by the time this job actually fires
        # (ordinary Python late-binding closure, same as `db` above).
        valorant_agent = ValorantAgent(
            profile_dir=config.valorant_profile_path,
            db=db,
        )
        scheduler.register(
            job_id="valorant_check",
            interval_minutes=config.valorant_interval_minutes,
            func=lambda: _valorant_cycle(valorant_agent, db, bot),
        )

        # No on/off config flag for SSO either, same reasoning as Teams/
        # VALORANT above - the persistent SSO profile is the gate: if
        # scripts/sso_login_setup.py hasn't been run locally yet, this
        # still starts fine and every cycle reports FAILING with
        # needs_reauth (agents/sso/agent.py catches its own auth errors).
        sso_agent = SsoAgent(
            profile_dir=config.sso_profile_path,
            db=db,
            session=sso_session,
        )
        scheduler.register(
            job_id="sso_check",
            interval_minutes=config.sso_interval_minutes,
            func=lambda: _sso_cycle(sso_agent, db, bot),
        )

        # Auth Checker Agent (bro's ТЗ) - shares teams_session/sso_session
        # with the two agents above (see the comment where those are
        # constructed), never opens a session of its own, and only ever
        # calls is_logged_in() - no login, no data fetch. Checker/Study
        # Manager/Mini App remain out of scope for this stage.
        auth_checker_agent = AuthCheckerAgent(
            db=db,
            teams_session=teams_session,
            sso_session=sso_session,
        )
        scheduler.register(
            job_id="auth_checker_check",
            interval_minutes=config.auth_checker_interval_minutes,
            func=lambda: _auth_checker_cycle(auth_checker_agent, db),
        )

        scheduler.start()

        if weather_agent is not None:
            # Run once immediately at startup, in addition to the
            # periodic job below - otherwise /weather would show nothing
            # for up to WEATHER_INTERVAL_MINUTES after every restart.
            # _weather_cycle already catches expected failures via
            # run_and_record/run_isolated, so this can't crash startup.
            await _weather_cycle(weather_agent, db)

        if config.telegram_bot_token and config.telegram_chat_id:
            bot = TelegramBot(config=config, db=db, wishlist=wishlist)
            await bot.start()
        else:
            logger.warning(
                "TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not set - running without the Telegram bot"
            )

        if teams_agent is not None:
            # Same immediate-first-run rationale as weather above - and
            # since this may open a real (headless) browser against a
            # profile that was never logged in (e.g. first-ever run),
            # _teams_cycle/run_and_record already turn that into a
            # FAILING AgentResult rather than letting it crash startup.
            # Placed AFTER the bot is constructed (unlike weather) so a
            # needs_reauth found on this very first run can still trigger
            # the Telegram notification right away, instead of silently
            # waiting for the next scheduled cycle.
            await _teams_cycle(teams_agent, db, bot)

        if valorant_agent is not None:
            # Same immediate-first-run rationale as weather/teams above -
            # placed AFTER the bot is constructed so that a needs_reauth
            # found on this very first run can still trigger the
            # Telegram notification right away, instead of silently
            # waiting for the next scheduled cycle.
            await _valorant_cycle(valorant_agent, db, bot)

        if sso_agent is not None:
            # Same immediate-first-run rationale as weather/teams/
            # valorant above - placed AFTER the bot is constructed so a
            # needs_reauth found on this very first run can still
            # trigger the Telegram notification right away.
            await _sso_cycle(sso_agent, db, bot)

        if auth_checker_agent is not None:
            # Same immediate-first-run rationale as the other agents
            # above - runs after teams_agent/sso_agent's own first cycle
            # so source_status reflects reality from the very first
            # moment /status (or a future Study Manager) could ask for
            # it, not just after the first scheduled interval elapses.
            await _auth_checker_cycle(auth_checker_agent, db)

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
        if valorant_agent is not None:
            try:
                await valorant_agent.aclose()
            except Exception:  # noqa: BLE001 - best-effort cleanup boundary
                logger.exception("Error while closing the VALORANT Agent's browser session")
        if sso_agent is not None:
            try:
                await sso_agent.aclose()
            except Exception:  # noqa: BLE001 - best-effort cleanup boundary
                logger.exception("Error while closing the SSO Agent's browser session")
        # teams_agent.aclose()/sso_agent.aclose() above are no-ops now
        # (session was injected, so _owns_session is False on both) -
        # run.py itself owns teams_session/sso_session (constructed
        # above so AuthCheckerAgent could share them), so it closes them
        # here instead. AuthCheckerAgent itself never opens a session of
        # its own and has no aclose() to call.
        try:
            await teams_session.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup boundary
            logger.exception("Error while closing the shared Teams browser session")
        try:
            await sso_session.close()
        except Exception:  # noqa: BLE001 - best-effort cleanup boundary
            logger.exception("Error while closing the shared SSO browser session")
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
