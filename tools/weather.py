"""get_weather tool (AI Manager ТЗ v3 §25).

Reads the latest already-collected weather_snapshots row - the same
source telegram_bot/handlers.py's own /weather command already reads -
rather than calling WeatherAgent.run() live on every chat message. A
live call is a real network round trip on every single read (spec
§84's "don't turn every technical operation into a long pipeline"),
and run.py's own scheduled weather_check job already keeps this
snapshot fresh (WEATHER_INTERVAL_MINUTES) - the same reasoning Study
Manager's own read methods already follow for Teams/SSO data.
"""
from __future__ import annotations

import json

from storage.database import Database
from tools.base import Tool


class ToolNotFoundError(Exception):
    """No weather snapshot has ever been saved yet - see tools/study.py's
    ToolNotFoundError for the same "unknown is an error, not silence"
    reasoning (spec §37)."""


def build_weather_tools(db: Database) -> list[Tool]:
    def _get_weather() -> dict:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT payload_json, created_at FROM weather_snapshots "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise ToolNotFoundError("no weather data collected yet")

        payload = json.loads(row["payload_json"])
        last_run = db.last_run("weather")
        # spec §38's freshness fields - AI Manager must be able to say
        # "this is from 18:00" rather than presenting a stored snapshot
        # as if it were live-just-fetched.
        return {
            **payload,
            "checked_at": row["created_at"],
            "last_run_status": last_run["status"] if last_run is not None else "UNKNOWN",
        }

    return [
        Tool(
            name="get_weather",
            description=(
                "Returns the most recently collected weather snapshot (current conditions plus "
                "a short forecast) for the configured location. Never invents weather data - if "
                "no snapshot has been collected yet, this tool returns an error instead of a "
                "guess."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_get_weather,
        )
    ]
