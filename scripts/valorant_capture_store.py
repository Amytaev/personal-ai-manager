"""One-off local script: investigate Stack B's real auth/network flow (Phase 6.1).

Run this yourself, once:

    python -m scripts.valorant_capture_store

WHY THIS EXISTS: bro's Phase 6.1 recon task - before writing a single
line of VALORANT Agent code, find out for REAL (not guessed) how
stackb.net (https://stackb.net/valorant/store) actually authenticates
against Riot and how it fetches your personal daily store. Same
"observe real traffic, don't assume the shape of an undocumented API"
discipline that Phase 5's Teams investigation used - and for good
reason: Teams taught us "the page's URL is not the API it actually
uses" the hard way, so this doesn't start by guessing either.

STRICT RULES THIS SCRIPT FOLLOWS (per bro's brief - read before running):
- This script NEVER touches, types, or stores your Riot password. When
  Stack B redirects you to Riot's own login page (login happens on
  Riot's side - Riot Sign-On/RSO, their real OAuth system, confirmed
  as the only officially supported way to get a player's own personal
  data - Riot's public match-history API explicitly does NOT cover
  store data), YOU type your password there by hand, in a real,
  visible browser window - exactly like logging into VALORANT
  normally. This script only watches network traffic that already
  happens because of your own login, the same way
  scripts/teams_capture_assignments.py only watched Teams' own
  traffic - it does not drive or automate that login in any way.
- No purchase, no write request of any kind is ever made by this
  script - it only opens pages and reads what your browser already
  receives.
- Session token VALUES are never printed or saved anywhere - only
  which storage mechanism holds a session (cookies / localStorage /
  sessionStorage) and the KEY NAMES, never the values. Same "don't
  leak a live credential into logs or chat" rule
  scripts/teams_agent_token_test.py already follows for the Teams work
  API's Bearer token.

What this captures, to valorant_capture/responses.jsonl (skim it
yourself before sending it back, same as every other capture in this
project - it's your own real account/store data):
  - every network response whose URL looks relevant (Stack B's own
    API, or any riotgames.com/valorant-api.com domain) while you log
    in and browse to your store page
  - on each Enter you press in this terminal: the current page URL,
    and which cookie/localStorage/sessionStorage KEY NAMES exist for
    the current page's origin (never the values)

After you've logged into Stack B with your real Riot account (by hand,
in the window this opens) and are looking at your real store there,
come back to this terminal and press Enter - then send back the
printed output plus the responses.jsonl file so the real data source
(or lack of one) can be understood before any VALORANT Agent code gets
written.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

STACKB_URL = "https://stackb.net/valorant/store"
PROFILE_DIR = Path("data/valorant_browser_profile")
CAPTURE_DIR = Path("valorant_capture")

# Broad and case-insensitive on purpose - same "capture wide, skim
# after" reasoning as teams_capture_assignments.py's
# INTERESTING_SUBSTRINGS. Covers Stack B's own API, Riot's known
# domains, and generic store/shop-shaped endpoint names.
INTERESTING_SUBSTRINGS = (
    "stackb.net/api", "stackb", "riotgames.com", "valorant-api.com",
    "storefront", "store", "shop",
)


def _looks_interesting(url: str) -> bool:
    lowered = url.lower()
    return any(s in lowered for s in INTERESTING_SUBSTRINGS)


async def main() -> None:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    responses_path = CAPTURE_DIR / "responses.jsonl"

    playwright = await async_playwright().start()
    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE_DIR),
        headless=False,
        viewport={"width": 1280, "height": 800},
    )
    page = await context.new_page()
    await page.goto(STACKB_URL, wait_until="domcontentloaded")

    captured = {"count": 0}

    async def on_response(response) -> None:
        if not _looks_interesting(response.url):
            return
        try:
            body: object
            try:
                body = await response.json()
            except Exception:  # noqa: BLE001 - not every response is JSON
                text = await response.text()
                body = text[:5000]  # cap size, this is a diagnostic capture not a full mirror
        except Exception as exc:  # noqa: BLE001 - never let the listener crash the capture
            body = f"<could not read body: {exc}>"

        entry = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "method": response.request.method,
            "url": response.url,
            "status": response.status,
            "body": body,
        }
        with responses_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        captured["count"] += 1
        print(f"  [captured #{captured['count']}] {response.status} {response.url[:110]}")

    page.on("response", lambda r: asyncio.create_task(on_response(r)))

    print("Browser window open at stackb.net/valorant/store.")
    print("Log in with YOUR Riot account by hand, in this window, the normal way -")
    print("this script never sees or touches your password.")
    print(f"Matching network responses are being saved to {responses_path}")
    print("Once you're looking at your real store on the page, press Enter here")
    print("to also record the current URL and which storage KEY NAMES (never")
    print("values) exist for this page's origin.\n")
    print("Press Ctrl+C here (or just close the browser) when you're done.\n")

    snapshot_count = 0
    try:
        while True:
            await asyncio.get_event_loop().run_in_executor(None, input, "")
            snapshot_count += 1
            storage_info = await page.evaluate(
                """
                () => ({
                    cookies_present: document.cookie.length > 0,
                    localStorage_keys: Object.keys(window.localStorage || {}),
                    sessionStorage_keys: Object.keys(window.sessionStorage || {}),
                })
                """
            )
            print(f"  [{snapshot_count}] URL: {page.url}")
            print(f"  [{snapshot_count}] cookies present: {storage_info['cookies_present']}")
            print(f"  [{snapshot_count}] localStorage keys: {storage_info['localStorage_keys']}")
            print(f"  [{snapshot_count}] sessionStorage keys: {storage_info['sessionStorage_keys']}")
            print()
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        await context.close()
        await playwright.stop()
        print(f"\nDone. {captured['count']} network response(s) saved under {CAPTURE_DIR}/")
        print("Please skim responses.jsonl before sending it back - it's your real account/store data.")


if __name__ == "__main__":
    asyncio.run(main())
