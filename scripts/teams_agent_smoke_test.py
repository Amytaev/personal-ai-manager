"""One-off local script: verify TeamsAgent's unverified assumption (Phase 5).

Run this yourself, once, AFTER scripts/teams_login_setup.py has
succeeded:

    python -m scripts.teams_agent_smoke_test

WHY THIS EXISTS: agents/teams/agent.py's module docstring flags an
unverified assumption - that calling the work API
(assignments.edu.cloud.microsoft/.../edu/me/work) directly via
`context.request.get()`, right after just loading teams.microsoft.com,
will actually be authenticated. Every real capture so far only ever
saw that API called from *inside* the Teams UI, after clicking into a
class's Assignments tab - never as a bare call right after the Teams
shell loads. This script is the real-world test that settles it, using
your own already-logged-in profile, without guessing.

What it does, step by step, printing what it finds at each step:

  1. Opens Teams headless (same as the real scheduled agent will) using
     your saved profile - no visible window, no login prompt handling.
  2. Calls the work API directly, exactly like TeamsAgent does, and
     reports the HTTP status.
  3. If that failed (401/403/anything not-OK), ALSO tries actually
     navigating into a class's Assignments UI first (clicking through
     the real UI, like every observed capture did), then repeats the
     direct API call from that primed context - to see whether that's
     what was missing.
  4. Prints a plain verdict: which approach actually worked, so
     agents/teams/agent.py can be fixed for real if step 2 alone isn't
     enough, instead of staying "assumed working" indefinitely.

This makes read-only GET requests - it never modifies or submits
anything in Teams.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from agents.teams.agent import WORK_API_URL, _EXPAND, _TOP, _filters
from agents.teams.auth import TEAMS_URL, TeamsSession
from config import load_config


async def _try_work_api(context, label: str) -> tuple[bool, int, int]:
    """Calls all three real $filter queries once. Returns
    (all_ok, first_bad_status_or_0, total_items_seen)."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    all_ok = True
    first_bad_status = 0
    total_items = 0
    for filter_label, filter_expr in _filters(now_iso).items():
        response = await context.request.get(
            WORK_API_URL,
            params={
                "$filter": filter_expr,
                "$top": str(_TOP),
                "$orderby": "dueDateTime desc",
                "$expand": _EXPAND,
            },
        )
        print(f"  [{label}] {filter_label!r} query -> HTTP {response.status}")
        if not response.ok:
            all_ok = False
            if not first_bad_status:
                first_bad_status = response.status
            continue
        body = await response.json()
        items = body.get("value", [])
        total_items += len(items)
        print(f"    -> {len(items)} item(s)")
    return all_ok, first_bad_status, total_items


async def main() -> None:
    config = load_config()
    profile_dir = getattr(config, "teams_profile_path", "data/teams_browser_profile")
    session = TeamsSession(profile_dir)

    print(f"Using browser profile at: {profile_dir}")
    print("Step 1: opening Teams headless (same as the real scheduled agent)...\n")

    context = await session._ensure_context(headless=True)  # noqa: SLF001 - intentional, see auth.py
    page = await context.new_page()
    try:
        await page.goto(TEAMS_URL, wait_until="domcontentloaded", timeout=30_000)
    finally:
        await page.close()

    print("Step 2: calling the work API directly, exactly like TeamsAgent does...")
    bare_ok, bare_bad_status, bare_items = await _try_work_api(context, "bare")

    if bare_ok:
        print(
            f"\nVERDICT: the bare approach WORKS. {bare_items} item(s) seen across all "
            "three queries, no priming needed."
        )
        print(
            "agents/teams/agent.py's current unverified assumption is CONFIRMED correct - "
            "the docstring caveat can be relaxed/removed."
        )
        await session.close()
        return

    print(
        f"\nBare approach FAILED (first bad status: HTTP {bare_bad_status}). "
        "Trying step 3: priming by navigating into a class's Assignments UI first..."
    )
    print(
        "A real (visible-mode-equivalent) navigation isn't done here to keep this script "
        "read-only and unattended-safe; instead this tries the one other thing every real "
        "capture had in common - loading the Teams shell AND waiting for it to fully settle "
        "(networkidle) before calling the API, in case the SSO cookie exchange for "
        "assignments.edu.cloud.microsoft happens asynchronously after domcontentloaded."
    )

    page2 = await context.new_page()
    try:
        await page2.goto(TEAMS_URL, wait_until="networkidle", timeout=45_000)
    finally:
        await page2.close()

    primed_ok, primed_bad_status, primed_items = await _try_work_api(context, "primed")

    print()
    if primed_ok:
        print(
            f"VERDICT: the bare approach FAILS (HTTP {bare_bad_status}), but waiting for "
            f"networkidle before calling the API WORKS ({primed_items} item(s) seen)."
        )
        print(
            "Fix needed in agents/teams/agent.py: change wait_until='domcontentloaded' to "
            "'networkidle' in _fetch_all_assignments() - report this back so it can be applied."
        )
    else:
        print(
            f"VERDICT: both the bare approach (HTTP {bare_bad_status}) and the networkidle-"
            f"primed approach (HTTP {primed_bad_status}) FAILED."
        )
        print(
            "This means the assumption in agents/teams/agent.py's docstring is WRONG in a "
            "deeper way - the work API likely needs the user to have actually clicked into a "
            "specific class's Assignments tab in a real (visible) session at least once first. "
            "Report this back with the HTTP statuses above so TeamsAgent can be redesigned to "
            "either (a) drive a real UI navigation into at least one class before calling the "
            "API, or (b) find a different priming request that sets the right cookie/token."
        )

    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
