"""One-off local script: test the Bearer-token hypothesis (Phase 5).

Run this yourself, once, AFTER scripts/teams_agent_navigation_test.py
showed that navigating into a class's Assignments iframe
(assignments.edu.cloud.microsoft/classes/<classId>/list) still leaves
the work API returning 401 for a cookie-only context.request.get()
call, even though the iframe itself clearly renders real assignment
data:

    python -m scripts.teams_agent_token_test

WHY THIS EXISTS: the iframe rendering real data while a cookie-only
API call still gets 401 means the work API is very likely NOT
cookie-authenticated at all - it's authenticated with a Bearer access
token in the `Authorization` header, obtained by the page's own MSAL.js
via silent SSO and kept in that origin's memory/sessionStorage, never
in an HTTP-only cookie `context.request` would pick up. This script
tests that directly: it navigates straight to the Assignments class
page (no manual clicking needed - it goes to a already-known classId
from agents/teams/parser.py's COURSE_NAMES), listens for the page's
OWN real request to the work API, and reuses the SAME Authorization
header for one of our own read-only requests - reusing a token the
browser already legitimately obtained for your own signed-in session,
the same "reuse what's already there" principle as the cookie-reuse
approach, just at the right layer this time.

SECURITY NOTE: the captured token is a live, short-lived bearer
credential for your own Microsoft account - this script NEVER prints
it (only a boolean "captured: yes/no" and the resulting HTTP status),
and never writes it to any file. Don't paste it anywhere either, same
as you wouldn't paste a password - if you see the raw token in a
future version of this script's output, that's a bug, stop and flag
it back rather than pasting it into chat.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from agents.teams.agent import WORK_API_URL, _EXPAND, _TOP, _filters
from agents.teams.auth import TeamsSession
from agents.teams.parser import COURSE_NAMES
from config import load_config

# Any already-known, resolved classId works for this test - it doesn't
# matter which course, we're only testing whether visiting a class's
# Assignments page yields a reusable Authorization header.
_SAMPLE_CLASS_ID = next(iter(COURSE_NAMES))


async def main() -> None:
    config = load_config()
    profile_dir = getattr(config, "teams_profile_path", "data/teams_browser_profile")
    session = TeamsSession(profile_dir)

    print(f"Using browser profile at: {profile_dir}")
    print(f"Navigating directly to the Assignments page for a known class ({_SAMPLE_CLASS_ID[:8]}...)")

    context = await session._ensure_context(headless=True)
    page = await context.new_page()

    captured_auth_header: str | None = None

    def _on_request(request) -> None:
        nonlocal captured_auth_header
        if captured_auth_header is not None:
            return
        if request.url.startswith(WORK_API_URL):
            header = request.headers.get("authorization")
            if header:
                captured_auth_header = header
                print("  -> captured an Authorization header on the page's own work-API request "
                      "(value withheld - see this script's security note)")

    page.on("request", _on_request)

    try:
        await page.goto(
            f"https://assignments.edu.cloud.microsoft/classes/{_SAMPLE_CLASS_ID}/list",
            wait_until="networkidle",
            timeout=30_000,
        )
        # Give the page's own JS a little extra time to fire its data
        # fetch in case networkidle settled just before it, since we
        # only care about the work-API request specifically.
        await page.wait_for_timeout(3_000)
    finally:
        await page.close()

    if captured_auth_header is None:
        print(
            "\nVERDICT: the class page loaded, but no request to the work API "
            f"({WORK_API_URL}) was observed at all within the wait window. "
            "Either this page doesn't call that exact endpoint, or it needs more "
            "time / a different classId. Report this back - a different diagnostic "
            "approach (checking sessionStorage/localStorage directly) would be next."
        )
        await session.close()
        return

    print("\nRe-testing the three real $filter queries WITH the captured Authorization header...")
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    all_ok = True
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
            headers={"authorization": captured_auth_header},
        )
        print(f"  {filter_label!r} query -> HTTP {response.status}")
        if not response.ok:
            all_ok = False
            continue
        body = await response.json()
        items = body.get("value", [])
        total_items += len(items)
        print(f"    -> {len(items)} item(s)")

    if all_ok:
        print(
            f"\nVERDICT: CONFIRMED - the work API is Bearer-token authenticated, not "
            f"cookie-authenticated. Reusing the token captured from the class page's own "
            f"request made all three queries succeed ({total_items} item(s) total). "
            "agents/teams/agent.py needs a real redesign: navigate to a known class's "
            "Assignments page first, capture the Authorization header the SAME way this "
            "script just did, then use it on the three $filter queries instead of relying "
            "on context.request's cookie jar alone."
        )
    else:
        print(
            "\nVERDICT: captured a header, but reusing it still failed on at least one "
            "query. Report this back with the statuses above - the token hypothesis might "
            "be right but something else differs (scope, audience, expiry) between the "
            "captured request and our replay."
        )

    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
