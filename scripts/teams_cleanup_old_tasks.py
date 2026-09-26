"""One-off local script: delete already-saved stale tasks (Phase 5).

Run this yourself, once, if your DB already has old/stale-semester
tasks saved from before agents/teams/agent.py started filtering by
TEAMS_MIN_DUE_DATE (config.py):

    python -m scripts.teams_cleanup_old_tasks

WHY THIS EXISTS: TeamsAgent now drops any assignment due before
TEAMS_MIN_DUE_DATE (default 2026-09-01) BEFORE saving it - but that
filter only applies going forward. A real run against the live tenant
already saved old-semester noise from an unresolved classId
(46191dc6) that turned out to be using "Задания" as an announcements
channel, all dated April 2026 - this cleans that (and anything else
like it) out of an existing DB without deleting anything else.

This only deletes rows from the `tasks` table whose due_at is before
the cutoff AND is not NULL - a task with no due date at all is left
alone (same "don't drop what we can't judge the age of" rule
agent.py's own filtering already follows).
"""
from __future__ import annotations

from config import load_config
from storage.database import Database


def main() -> None:
    config = load_config()
    db = Database(config.database_path)
    cutoff_iso = config.teams_min_due_date.isoformat()

    with db.connect() as conn:
        rows = conn.execute(
            "SELECT id, course, title, due_at FROM tasks "
            "WHERE due_at IS NOT NULL AND due_at < ?",
            (cutoff_iso,),
        ).fetchall()

        if not rows:
            print(f"No tasks due before {cutoff_iso} found - nothing to clean up.")
            return

        print(f"Found {len(rows)} task(s) due before {cutoff_iso}:")
        for row in rows:
            print(f"  [{row['course']}] {row['title']} (due {row['due_at']})")

        answer = input(f"\nDelete these {len(rows)} task(s)? [y/N] ").strip().lower()
        if answer != "y":
            print("Aborted - nothing deleted.")
            return

        cur = conn.execute(
            "DELETE FROM tasks WHERE due_at IS NOT NULL AND due_at < ?",
            (cutoff_iso,),
        )
        print(f"Deleted {cur.rowcount} task(s).")


if __name__ == "__main__":
    main()
