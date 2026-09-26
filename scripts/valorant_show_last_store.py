"""One-off local script: print the last saved VALORANT store row.

Run:

    python -m scripts.valorant_show_last_store

Quick sanity check that agents/valorant/agent.py's run actually wrote
something real to the DB - no agent/network calls here, just reads
storage/database.py's valorant_store table.
"""
from __future__ import annotations

from config import load_config
from storage.database import Database


def main() -> None:
    config = load_config()
    db = Database(config.database_path)
    with db.connect() as conn:
        row = conn.execute(
            "SELECT skins_json, checked_at FROM valorant_store ORDER BY id DESC LIMIT 1"
        ).fetchone()

    if row is None:
        print("No valorant_store rows yet - the agent hasn't saved anything.")
        return

    print(f"checked_at: {row['checked_at']}")
    print(row["skins_json"])


if __name__ == "__main__":
    main()
