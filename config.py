"""Central configuration loader (TZ v4 §3.1 / §5).

Reads everything from environment variables (populated from .env via
python-dotenv). No default value here is ever a real secret - empty
string / safe numeric defaults only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()


def _int_env(key: str, default: int) -> int:
    value = os.getenv(key)
    return int(value) if value else default


def _csv_env(key: str) -> tuple[str, ...]:
    raw = os.getenv(key, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def _date_env(key: str, default_iso: str) -> datetime:
    raw = os.getenv(key) or default_iso
    # Same "Z" -> "+00:00" normalization agents/teams/parser.py's
    # parse_due() already uses for the API's own dueDateTime values.
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.fromisoformat(default_iso.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        # A bare "2026-09-01" (no time/offset) in .env is still a
        # reasonable thing to write - assume UTC rather than crash,
        # since it's compared against aware dueDateTime values later.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass(frozen=True)
class AppConfig:
    telegram_bot_token: str
    telegram_chat_id: str

    weather_location: str
    weather_api_key: str

    llm_provider: str
    llm_model: str
    llm_api_key: str

    teams_interval_minutes: int
    weather_interval_minutes: int
    valorant_interval_minutes: int
    sso_interval_minutes: int
    auth_checker_interval_minutes: int
    checker_interval_minutes: int
    checker_window_start_days: int
    checker_window_end_days: int
    study_manager_interval_minutes: int
    briefing_hour: int

    included_courses: tuple[str, ...]
    excluded_courses: tuple[str, ...]

    data_retention_days: int

    database_path: str
    log_dir: str
    lock_file_path: str
    wishlist_path: str
    teams_profile_path: str
    teams_min_due_date: datetime
    valorant_profile_path: str
    sso_profile_path: str
    semester1_week1_start: datetime


def load_config() -> AppConfig:
    return AppConfig(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        weather_location=os.getenv("WEATHER_LOCATION", ""),
        weather_api_key=os.getenv("WEATHER_API_KEY", ""),
        llm_provider=os.getenv("LLM_PROVIDER", "anthropic"),
        llm_model=os.getenv("LLM_MODEL", ""),
        llm_api_key=os.getenv("LLM_API_KEY", ""),
        teams_interval_minutes=_int_env("TEAMS_INTERVAL_MINUTES", 60),
        weather_interval_minutes=_int_env("WEATHER_INTERVAL_MINUTES", 60),
        valorant_interval_minutes=_int_env("VALORANT_INTERVAL_MINUTES", 120),
        # No live-observed cadence yet (unlike Teams/VALORANT's real
        # usage history) - defaults to the same 120 minutes as VALORANT
        # for now; schedule/UMKD data changes far less often than an
        # assignment list, so this is deliberately not aggressive.
        sso_interval_minutes=_int_env("SSO_INTERVAL_MINUTES", 120),
        # Auth Checker only ever does a lightweight is_logged_in() read
        # (no login, no data fetch) on sessions the real source agents
        # already own (see agents/auth_checker.py's docstring on session
        # sharing) - cheap enough to run more often than the heavier
        # Teams/SSO sync cycles above, so a stale session is flagged in
        # source_status well before the next real sync would have
        # noticed it on its own.
        auth_checker_interval_minutes=_int_env("AUTH_CHECKER_INTERVAL_MINUTES", 30),
        # Checker Agent (bro's ТЗ) only ever reads already-stored rows
        # (sso_courses/sso_schedule_entries/tasks/source_status) - no
        # network, no browser - so it's cheap enough to run on roughly
        # the same cadence as the SSO/Teams syncs it depends on, without
        # needing to be nearly as frequent as Auth Checker's own
        # lightweight is_logged_in() probe.
        checker_interval_minutes=_int_env("CHECKER_INTERVAL_MINUTES", 120),
        # Checker's spec §7 default window - how far back/forward from
        # "now" a schedule occurrence or a Teams assignment's due date
        # is even considered, so an old assignment from a past semester
        # (or a schedule occurrence far in the future) never gets
        # matched against something unrelated just because the course
        # happens to line up.
        checker_window_start_days=_int_env("CHECKER_WINDOW_START_DAYS", 7),
        checker_window_end_days=_int_env("CHECKER_WINDOW_END_DAYS", 30),
        # Study Manager (bro's ТЗ) only ever reads already-stored rows
        # for its own get_*() methods (spec §27 - no network, no
        # browser there at all); a scheduled refresh() is what actually
        # drives Teams/SSO/Auth Checker/Checker's own real syncs, so
        # this interval is deliberately the same order of magnitude as
        # sso_interval_minutes/checker_interval_minutes above rather
        # than auth_checker's much cheaper 30-minute cadence.
        study_manager_interval_minutes=_int_env("STUDY_MANAGER_INTERVAL_MINUTES", 120),
        briefing_hour=_int_env("BRIEFING_HOUR", 8),
        included_courses=_csv_env("INCLUDED_COURSES"),
        excluded_courses=_csv_env("EXCLUDED_COURSES"),
        data_retention_days=_int_env("DATA_RETENTION_DAYS", 30),
        database_path=os.getenv("DATABASE_PATH", "data/agent.db"),
        log_dir=os.getenv("LOG_DIR", "logs"),
        lock_file_path=os.getenv("LOCK_FILE_PATH", "data/.instance.lock"),
        wishlist_path=os.getenv("WISHLIST_PATH", "config/wishlist.json"),
        teams_profile_path=os.getenv("TEAMS_PROFILE_PATH", "data/teams_browser_profile"),
        # Same default as scripts/valorant_capture_store.py's PROFILE_DIR
        # (the Phase 6.1 investigation script) - reuses that already
        # logged-in session by default instead of forcing a second login.
        valorant_profile_path=os.getenv("VALORANT_PROFILE_PATH", "data/valorant_browser_profile"),
        # Same default as scripts/sso_login_setup.py/sso_capture_data.py's
        # PROFILE_DIR - reuses that already-logged-in, separate profile
        # (never mixed with Teams/VALORANT's own, per this project's
        # standing rule).
        sso_profile_path=os.getenv("SSO_PROFILE_PATH", "data/sso_browser_profile"),
        # Assignments due before this are dropped by TeamsAgent before
        # anything is saved to the DB - stale/old-semester noise
        # (confirmed for real: the unresolved classId 46191dc6 turned
        # out to be an old course using "Задания" as an announcements
        # channel, all dated April 2026). Defaults to the start of the
        # user's current semester; override in .env if that changes.
        teams_min_due_date=_date_env("TEAMS_MIN_DUE_DATE", "2026-09-01T00:00:00Z"),
        # utils/academic_calendar.py's anchor for teaching week 1 - a
        # separate var from TEAMS_MIN_DUE_DATE above even though they
        # currently share the same real-world date, since they're
        # different concepts (a task-filtering cutoff vs. the week
        # counter's anchor) that could diverge - e.g. TEAMS_MIN_DUE_DATE
        # might get bumped for a reason unrelated to the calendar rule.
        semester1_week1_start=_date_env("SEMESTER1_WEEK1_START", "2026-09-01T00:00:00Z"),
    )
