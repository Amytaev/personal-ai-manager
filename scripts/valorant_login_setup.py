"""One-off local script: set up the persistent Stack B login (Phase 6).

Run this yourself, once, on your own machine (and again any time the
VALORANT Agent's /status turns red with "needs_reauth" / a Telegram
notification says the Stack B session expired):

    python -m scripts.valorant_login_setup

A real Chromium window will open at stackb.net's store page. Sign in
with your Riot account by hand, exactly like logging into VALORANT
normally - Stack B redirects the actual credential entry to Riot's own
page, and this script never sees or touches your password. It only
waits and watches which URL you land on afterwards.

This reuses the same persistent browser profile
scripts/valorant_capture_store.py already used for the Phase 6.1
investigation (config.py's VALORANT_PROFILE_PATH,
data/valorant_browser_profile by default) - if that earlier session is
still valid, this script's headless re-check below will simply confirm
it's already fine.
"""
from __future__ import annotations

import asyncio
import sys

from agents.valorant.auth import ValorantLoginTimeout, ValorantSession
from config import load_config


async def main() -> None:
    config = load_config()
    profile_dir = config.valorant_profile_path
    session = ValorantSession(profile_dir)

    print(f"Using browser profile at: {profile_dir}")
    print("Opening a real browser window - please sign in (via Riot) by hand (up to 10 minutes)...")

    try:
        final_url = await session.login_interactively(timeout_seconds=600)
    except ValorantLoginTimeout as exc:
        print(f"\nLogin was not detected as complete: {exc}")
        print("If you DID finish logging in, send back whatever URL your browser ended up on.")
        await session.close()
        sys.exit(1)

    print(f"\nLogin detected as successful. Final URL was:\n  {final_url}")

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
    print("The VALORANT Agent (agents/valorant/agent.py) will reuse this session on its own.")


if __name__ == "__main__":
    asyncio.run(main())
