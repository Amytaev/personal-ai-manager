"""Central configuration loader (TZ v4 §3.1 / §5).

Reads everything from environment variables (populated from .env via
python-dotenv). No default value here is ever a real secret - empty
string / safe numeric defaults only.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int_env(key: str, default: int) -> int:
    value = os.getenv(key)
    return int(value) if value else default


def _csv_env(key: str) -> tuple[str, ...]:
    raw = os.getenv(key, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


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
    briefing_hour: int

    included_courses: tuple[str, ...]
    excluded_courses: tuple[str, ...]

    data_retention_days: int

    database_path: str
    log_dir: str
    lock_file_path: str
    wishlist_path: str


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
        briefing_hour=_int_env("BRIEFING_HOUR", 8),
        included_courses=_csv_env("INCLUDED_COURSES"),
        excluded_courses=_csv_env("EXCLUDED_COURSES"),
        data_retention_days=_int_env("DATA_RETENTION_DAYS", 30),
        database_path=os.getenv("DATABASE_PATH", "data/agent.db"),
        log_dir=os.getenv("LOG_DIR", "logs"),
        lock_file_path=os.getenv("LOCK_FILE_PATH", "data/.instance.lock"),
        wishlist_path=os.getenv("WISHLIST_PATH", "config/wishlist.json"),
    )
