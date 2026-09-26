"""One-off local script: capture real Teams Assignments data (Phase 5).

Run this yourself, once, AFTER scripts/teams_login_setup.py has
succeeded:

    python -m scripts.teams_capture_assignments

A real (visible) Chromium window opens using your already-logged-in
profile. Manually click into a class team, open its Assignments tab,
and browse the Upcoming/Overdue/Returned lists a bit - the script
listens in the background and saves:

  1. every network response whose URL looks assignment-related, to
     teams_capture/responses.jsonl
  2. a snapshot of the page's HTML whenever you press Enter in this
     terminal (do this while looking at an interesting Assignments
     view), to teams_capture/snapshot_<N>.html

When you're done, close the browser window or press Ctrl+C here.

WHY THIS EXISTS: this project was written in a sandbox with no network
access to teams.microsoft.com, so the actual structure of Teams'
Assignments data (JSON API shape, or DOM layout if there's no clean
API) is genuinely unknown here. Rather than guessing and shipping a
parser that silently breaks on real data, this captures the ground
truth once so the real parser can be written against it.

BEFORE SENDING THE CAPTURED FILES BACK: skim them yourself first. It's
your own real assignment/course data - the capture is scoped to
assignment-looking URLs only (not your whole Teams session), but you
know your own data better than any filter here does.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from agents.teams.auth import TEAMS_URL, TeamsSession
from config import load_config

CAPTURE_DIR = Path("teams_capture")
# Deliberately broad and case-insensitive - better to capture a bit too
# much (and skim/discard) than to miss the one endpoint that matters
# because of an overly narrow guess.
INTERESTING_SUBSTRINGS = ("assignment", "gradebook", "grade", "coursework", "submission")


def _looks_interesting(url: str) -> bool:
    lowered = url.lower()
    return any(s in lowered for s in INTERESTING_SUBSTRINGS)


async def main() -> None:
    config = load_config()
    profile_dir = getattr(config, "teams_profile_path", "data/teams_browser_profile")
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    responses_path = CAPTURE_DIR / "responses.jsonl"

    session = TeamsSession(profile_dir)
    # Reach past TeamsSession's own headless-only is_logged_in()/
    # login_interactively() helpers straight to a visible context, since
    # this script needs the user to freely click around, not just watch
    # a fixed login flow.
    context = await session._ensure_context(headless=False)  # noqa: SLF001 - intentional, see above
    page = await context.new_page()
    # BUG FIX: a freshly opened page starts on about:blank. Without this
    # goto, the visible window has nothing in it and there's nothing for
    # the user to click into - confirmed by a real run where the window
    # opened blank and then closed. Navigate to Teams itself so the saved
    # login session kicks in and the user lands on their actual Teams UI.
    await page.goto(TEAMS_URL, wait_until="domcontentloaded")

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
        print(f"  [captured #{captured['count']}] {response.status} {response.url[:100]}")

    page.on("response", lambda r: asyncio.create_task(on_response(r)))

    print("Browser window open. Navigate to a class Team -> Assignments tab.")
    print(f"Matching network responses are being saved to {responses_path}")
    print("Press Enter at any point to also save the current page's HTML as a snapshot.")
    print("Press Ctrl+C here (or just close the browser) when you're done.\n")

    snapshot_count = 0
    try:
        while True:
            await asyncio.get_event_loop().run_in_executor(None, input, "")
            snapshot_count += 1
            html = await page.content()
            snapshot_path = CAPTURE_DIR / f"snapshot_{snapshot_count}.html"
            snapshot_path.write_text(html, encoding="utf-8")
            print(f"  Saved DOM snapshot: {snapshot_path} (current URL: {page.url})")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        await session.close()
        print(f"\nDone. {captured['count']} network responses, {snapshot_count} DOM snapshots saved under {CAPTURE_DIR}/")
        print("Please skim these before sending them back - it's your real course data.")


if __name__ == "__main__":
    asyncio.run(main())
