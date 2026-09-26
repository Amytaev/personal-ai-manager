"""Diagnostic script (Phase 6.3, third round): why does TeamsAgent hit a
GENUINELY BLANK Microsoft "Sign in" screen (empty email field, no account
remembered at all - confirmed by a real screenshot, 2026-09-26) in
headless mode, sometimes within seconds of a confirmed-successful
interactive login?

That screenshot ruled out the previous "the redirect just needs more
time" theory - this isn't a silent-refresh-in-progress page, it's a
genuinely fresh, nobody's-logged-in-here sign-in form. Two real
possibilities remain, and this script tells them apart with actual data
instead of another guess:

1. The persistent profile's Microsoft/Azure AD cookies never make it into
   a HEADLESS launch at all (a cookie-persistence problem specific to
   headless mode) - in which case even a HEADED (visible) window reusing
   the exact same profile, with NO fresh login, would show the same blank
   sign-in screen.
2. The cookies ARE there in both modes, but something about how headless
   Chromium presents itself to Microsoft's login flow makes the server
   reject/ignore them and force a fresh interactive sign-in (a
   detection/Conditional-Access-style issue) - in which case a HEADED
   window on the same profile, no fresh login, would still show a normal
   logged-in Teams session.

Run this RIGHT AFTER scripts/teams_login_setup.py reports a successful
interactive login, without doing anything else in between:

    python -m scripts.teams_diagnose_reauth

It does NOT touch your password and does NOT log in on its own - it only
opens the existing profile twice (once headless, once in a normal visible
window) and reports, for each: the final URL, whether a login form is
visible, and which Microsoft/Azure AD cookie NAMES exist for the relevant
domains (never values - a cookie value is a live session credential and
is deliberately never printed or saved anywhere).
"""
from __future__ import annotations

import asyncio

from agents.teams.auth import LOGGED_IN_URL_HINT, TEAMS_URL, TeamsSession
from config import load_config

# Cookie domains worth reporting on - Microsoft's login/session cookies
# live under these, not under teams.microsoft.com itself.
_RELEVANT_DOMAIN_SUBSTRINGS = ("microsoftonline.com", "microsoft.com", "live.com", "msauth")


async def _inspect(session: TeamsSession, *, headless: bool, label: str) -> None:
    context = await session._ensure_context(headless=headless)  # noqa: SLF001 - diagnostic script
    page = await context.new_page()
    try:
        await page.goto(TEAMS_URL, wait_until="domcontentloaded", timeout=30_000)
        # Give any redirect a real chance to settle either way before
        # reading the final state - same tolerance the real agent now uses.
        try:
            await page.wait_for_url(f"**{LOGGED_IN_URL_HINT}**", timeout=20_000)
        except Exception:  # noqa: BLE001 - handled by reporting page.url below either way
            pass

        final_url = page.url
        looks_logged_in = LOGGED_IN_URL_HINT in final_url

        cookies = await context.cookies()
        relevant = [
            c for c in cookies
            if any(sub in c.get("domain", "") for sub in _RELEVANT_DOMAIN_SUBSTRINGS)
        ]

        print(f"\n--- {label} (headless={headless}) ---")
        print(f"Final URL: {final_url}")
        print(f"Looks logged in (matches {LOGGED_IN_URL_HINT!r}): {looks_logged_in}")
        print(f"Relevant cookie count: {len(relevant)}")
        for c in relevant:
            # Name + domain + expiry only - NEVER the value, that's a live
            # session credential.
            print(f"  - {c.get('name')} @ {c.get('domain')} (expires: {c.get('expires')})")
        if not looks_logged_in:
            screenshot_path = f"data/teams_diagnose_{label}.png"
            await page.screenshot(path=screenshot_path)
            print(f"Saved a screenshot to {screenshot_path} for a visual check too.")
    finally:
        await page.close()


async def main() -> None:
    config = load_config()
    profile_dir = getattr(config, "teams_profile_path", "data/teams_browser_profile")
    session = TeamsSession(profile_dir)

    print(f"Using existing browser profile at: {profile_dir}")
    print("Not logging in - just inspecting the session that's already there.\n")

    await _inspect(session, headless=True, label="headless")
    await _inspect(session, headless=False, label="headed")

    await session.close()

    print(
        "\nSend both blocks above back (still no cookie VALUES in there, just names/domains) - "
        "that tells us whether this is a headless-only cookie problem or something else."
    )


if __name__ == "__main__":
    asyncio.run(main())
