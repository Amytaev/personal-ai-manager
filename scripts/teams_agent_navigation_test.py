"""One-off local script: find what actually primes the work API (Phase 5).

Run this yourself, once, AFTER scripts/teams_agent_smoke_test.py showed
HTTP 401 for both the bare and networkidle-primed approaches:

    python -m scripts.teams_agent_navigation_test

WHY THIS EXISTS: the smoke test (scripts/teams_agent_smoke_test.py)
disproved the assumption in agents/teams/agent.py's docstring - just
loading teams.microsoft.com, with or without waiting for networkidle,
does NOT get assignments.edu.cloud.microsoft/.../edu/me/work to
authenticate (401 on all three $filter queries). The remaining
hypothesis is that the SSO cookie/token for that origin only gets set
once you've actually navigated into a specific class's Assignments UI
- but no real capture so far recorded *what URL that navigation lands
on*, only the network responses that happen once you're there. This
script gets that missing piece of ground truth, the same way
teams_login_setup.py got LOGGED_IN_URL_HINT: by watching a real
session and reporting exactly what happened, instead of guessing.

What it does:

  1. Opens a REAL, VISIBLE Chromium window using your saved profile
     (same as teams_capture_assignments.py) - you manually click into
     any one class, then open its Assignments tab.
  2. When you press Enter in this terminal (do this once you're
     looking at that class's Assignments list), the script:
       a. prints the current page URL (this is the missing piece -
          the real navigation path agent.py would need to replicate),
       b. immediately calls the same three work-API $filter queries
          agent.py uses, from that SAME browser context, and reports
          each one's HTTP status.
  3. Prints a plain verdict: did navigating into the class actually
     unlock the API, or is something else still missing.

This makes read-only GET requests - it never modifies or submits
anything in Teams. Close the window or press Ctrl+C when you're done.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from agents.teams.agent import WORK_API_URL, _EXPAND, _TOP, _filters
from agents.teams.auth import TEAMS_URL, TeamsSession
from config import load_config


async def _try_work_api(context) -> tuple[bool, int, int]:
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
        print(f"    {filter_label!r} query -> HTTP {response.status}")
        if not response.ok:
            all_ok = False
            if not first_bad_status:
                first_bad_status = response.status
            continue
        body = await response.json()
        items = body.get("value", [])
        total_items += len(items)
        print(f"      -> {len(items)} item(s)")
    return all_ok, first_bad_status, total_items


async def main() -> None:
    config = load_config()
    profile_dir = getattr(config, "teams_profile_path", "data/teams_browser_profile")
    session = TeamsSession(profile_dir)

    print(f"Using browser profile at: {profile_dir}")
    # Reach past TeamsSession's headless-only helpers straight to a
    # visible context - same reasoning as teams_capture_assignments.py,
    # this needs you to freely click around.
    context = await session._ensure_context(headless=False)  # noqa: SLF001 - intentional, see auth.py
    page = await context.new_page()
    await page.goto(TEAMS_URL, wait_until="domcontentloaded")

    print("\nBrowser window open. Click into any ONE class, then open its Assignments tab.")
    print("Once you're looking at that class's Assignments list, press Enter here.\n")
    print("Press Ctrl+C here (or just close the browser) when you're done.\n")

    attempt = 0
    try:
        while True:
            await asyncio.get_event_loop().run_in_executor(None, input, "")
            attempt += 1
            current_url = page.url
            print(f"\n[attempt {attempt}] Top-level page URL:\n  {current_url}")

            # Teams renders "personal apps" like Assignments inside an
            # iframe on a DIFFERENT origin - page.url only ever shows the
            # top-level teams.cloud.microsoft URL, never the iframe's own
            # URL, which is very likely where the real navigation (and
            # the SSO cookie exchange for assignments.edu.cloud.microsoft)
            # actually happens. List every frame on every open tab so the
            # real origin shows up here instead of staying invisible.
            all_pages = context.pages
            print(f"[attempt {attempt}] Open tab(s): {len(all_pages)}")
            for page_index, p in enumerate(all_pages):
                print(f"  tab {page_index}: {p.url}")
                for frame in p.frames:
                    if frame.url and frame.url != p.url:
                        print(f"    frame: {frame.url}")

            print(f"[attempt {attempt}] Calling the work API from this context...")
            ok, bad_status, items = await _try_work_api(context)
            if ok:
                print(
                    f"\n[attempt {attempt}] VERDICT: WORKS after this navigation "
                    f"({items} item(s) seen). Save this URL and send it back - "
                    "agent.py needs to navigate to something with this shape "
                    "before calling the API."
                )
            else:
                print(
                    f"\n[attempt {attempt}] VERDICT: still failing (HTTP {bad_status}) even "
                    "after this navigation. Either this isn't the right page yet, or the "
                    "cookie exchange needs more time - try waiting a few seconds on the "
                    "Assignments tab and pressing Enter again."
                )
            print("\nPress Enter again to retry from the current page, or Ctrl+C to stop.\n")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        await session.close()
        print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
