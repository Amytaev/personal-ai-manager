"""get_valorant_store tool (AI Manager ТЗ v3 §26).

Reads the latest already-collected valorant_store row - the same
source telegram_bot/handlers.py's own /store command already reads -
rather than calling ValorantAgent.run() live on every chat message:
that opens a real, heavy Playwright browser context against Stack B
(agents/valorant/agent.py), far too slow for an ordinary read and
exactly the kind of unnecessary long pipeline spec §84 warns against.
"""
from __future__ import annotations

import json

from storage.database import Database
from tools.base import Tool


class ToolNotFoundError(Exception):
    """No VALORANT store snapshot has ever been saved yet."""


class StoreUnavailableError(Exception):
    """Store data exists as a row, but its payload doesn't actually
    contain any real items - spec §26's explicit rule: STORE_UNAVAILABLE
    must never be turned into "сегодня магазин пустой", so this is
    raised (and reported as a real ToolResult.fail()) rather than
    quietly returning an empty item list that reads the same as "no
    skins today"."""


def build_valorant_tools(db: Database) -> list[Tool]:
    def _get_valorant_store() -> dict:
        with db.connect() as conn:
            row = conn.execute(
                "SELECT skins_json, checked_at FROM valorant_store "
                "ORDER BY checked_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            raise ToolNotFoundError("no VALORANT store data collected yet")

        try:
            payload = json.loads(row["skins_json"])
            items = payload.get("items") if isinstance(payload, dict) else None
        except ValueError:
            items = None
        if not isinstance(items, list) or not items:
            raise StoreUnavailableError("STORE_UNAVAILABLE")

        last_run = db.last_run("valorant")
        return {
            "items": items,
            "reset_in": payload.get("reset_in"),
            "checked_at": row["checked_at"],
            "last_run_status": last_run["status"] if last_run is not None else "UNKNOWN",
        }

    return [
        Tool(
            name="get_valorant_store",
            description=(
                "Returns today's VALORANT daily storefront (skins + prices in VP) from the most "
                "recently collected snapshot. If the store is unavailable, this tool returns an "
                "error - never reports an empty store as if that were real, current data."
            ),
            parameters={"type": "object", "properties": {}, "required": []},
            handler=_get_valorant_store,
        )
    ]
