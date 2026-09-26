"""Diagnostic script (Phase 6.3, third round): why does TeamsAgent hit a
GENUINELY BLANK Microsoft "Sign in" screen (empty email field, no account
remembered at all - confirmed by a real screenshot, 2026-09-26) in
headless mode, sometimes within seconds of a confirmed-successful
interactive login?

CORRECTION (same day, after a 5th live run): this script's first version
used page.wait_for_url() right after goto(), which is a NO-OP bug - see
agents/teams/auth.py's TeamsSession.is_logged_in() docstring for the full
story. That bug made this script's very first run report a false "looks
logged in: True" for both headless and headed, seconds before a real
agent run hit the same blank sign-in form on the identical profile - the
"cookies are fine in both modes" conclusion drawn from that run was an
artifact of the bug, not real evidence either way. Fixed below to wait
for the network to actually settle before reading the final state, same
fix as is_logged_in(). Re-run this if the headless-vs-headed question
still matters once TeamsAgent itself is behaving reliably again.

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
        # Give any client-side redirect a real chance to happen before
        # reading the final state - waiting for network idle actually
        # waits; comparing the URL to itself right after goto() (the
        # original version of this script) does not, see this file's
        # docstring.
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
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
