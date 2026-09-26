"""One-off local RESEARCH script, part 3 (Этап A continued): capture the
REAL network traffic behind stud.satbayev.university's schedule, УМКД,
a specific subject, and any materials/lectures/labs/practicals - so the
real SSO Agent (Этап B) can be built against actual response shapes
instead of guessed ones.

Run this yourself, AFTER scripts/sso_login_setup.py has succeeded (reuses
the same profile - no fresh login, no password touched here):

    python -m scripts.sso_capture_data

A real (visible) Chromium window opens on your already-authenticated
session. Manually browse around:

  1. the schedule page (student-schedule);
  2. the УМКД page (umkd-student);
  3. open a specific subject from either;
  4. open any materials/lectures/labs/practicals you can reach from there.

The script listens in the background and saves:

  1. every network response whose URL looks relevant (schedule/УМКД/
     course/material-ish keywords - deliberately broad, see
     INTERESTING_SUBSTRINGS below) to sso_capture/responses.jsonl - this
     is how we find out whether there's a clean internal JSON API behind
     the Angular-style hash routing (#!/...) or whether it's server-
     rendered HTML we'd have to parse instead.
  2. a snapshot of the page's HTML whenever you press Enter in this
     terminal (do this while looking at an interesting view), to
     sso_capture/snapshot_<N>.html - useful as a fallback if a given
     screen turns out to have no clean API of its own.

Press Ctrl+C here (or just close the browser) when you're done.

BEFORE SENDING THE CAPTURED FILES BACK: skim them yourself first - it's
your own real schedule/course/material data. The capture is scoped to
schedule/УМКД/course-looking URLs only (not your whole session), but you
know your own data better than any filter here does.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

PROFILE_DIR = "data/sso_browser_profile"
START_URL = "https://stud.satbayev.university/#!/82/student-schedule"
CAPTURE_DIR = Path("sso_capture")

# Deliberately broad and case-insensitive - better to capture a bit too
# much (and skim/discard) than to miss the one endpoint that matters
# because of an overly narrow guess (same lesson learned building
# scripts/teams_capture_assignments.py).
INTERESTING_SUBSTRINGS = (
    "schedule", "umkd", "course", "subject", "discipline", "material",
    "lecture", "lab", "practic", "api", "student",
)


def _looks_interesting(url: str) -> bool:
    lowered = url.lower()
    return any(s in lowered for s in INTERESTING_SUBSTRINGS)


async def main() -> None:
    CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
    responses_path = CAPTURE_DIR / "responses.jsonl"

    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()
        await page.goto(START_URL, wait_until="domcontentloaded")

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

        print("Browser window open, starting on the schedule page.")
        print("Browse: schedule -> УМКД -> a specific subject -> any materials you can reach.")
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
            await context.close()
            print(
                f"\nDone. {captured['count']} network responses, {snapshot_count} DOM "
                f"snapshots saved under {CAPTURE_DIR}/"
            )
            print("Please skim these before sending them back - it's your real schedule/course data.")


if __name__ == "__main__":
    asyncio.run(main())
