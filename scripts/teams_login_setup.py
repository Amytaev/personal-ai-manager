"""One-off local script: set up the persistent Teams login (Phase 5).

Run this yourself, once, on your own machine:

    python -m scripts.teams_login_setup

A real Chromium window will open. Sign in with your KazNITU/Satbayev
Microsoft account by hand - including MFA if it asks. This script
never sees or touches your password; it only waits and watches which
URL you land on afterwards.

What this is actually for: LOGGED_IN_URL_HINT in agents/teams/auth.py
is currently a guess (written without network access to
teams.microsoft.com). This script prints the REAL url Playwright saw
once you were logged in - paste that back so the guess can be
corrected with real data instead of assumed.
"""
from __future__ import annotations

import asyncio
import sys

from agents.teams.auth import TeamsLoginTimeout, TeamsSession
from config import load_config


async def main() -> None:
    config = load_config()
    profile_dir = getattr(config, "teams_profile_path", "data/teams_browser_profile")
    session = TeamsSession(profile_dir)

    print(f"Using browser profile at: {profile_dir}")
    print("Opening a real browser window - please sign in by hand (up to 10 minutes)...")

    try:
        final_url = await session.login_interactively(timeout_seconds=600)
    except TeamsLoginTimeout as exc:
        print(f"\nLogin was not detected as complete: {exc}")
        print("If you DID finish logging in, the URL-matching guess in")
        print("agents/teams/auth.py (LOGGED_IN_URL_HINT) is probably wrong -")
        print("send back whatever URL your browser ended up on.")
        await session.close()
        sys.exit(1)

    print(f"\nLogin detected as successful. Final URL was:\n  {final_url}")
    print("\nSend that URL back so LOGGED_IN_URL_HINT can be corrected if needed.")

    print("\nDouble-checking the saved session works headless (like the real agent will use it)...")
    still_logged_in = await session.is_logged_in()
    print(f"Headless re-check: {'OK, still logged in' if still_logged_in else 'FAILED - see note below'}")
    if not still_logged_in:
        print(
            "This would mean the session doesn't survive between a visible and a headless "
            "run - worth flagging back, this affects how the real scheduled agent works."
        )

    await session.close()
    print(f"\nProfile saved at: {profile_dir}")
    print("You can now run scripts/teams_capture_assignments.py to help build the actual parser.")


if __name__ == "__main__":
    asyncio.run(main())
