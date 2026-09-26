"""One-off local script: drive the real UI click instead of guessing a URL (Phase 5).

Run this yourself, once, AFTER scripts/teams_agent_token_test.py showed
that navigating straight to assignments.edu.cloud.microsoft/classes/<id>
/list never even fires a request to the work API - the app just sits
there waiting for a Teams SDK handshake with a real parent Teams shell
that a bare page.goto() to that URL never provides:

    python -m scripts.teams_agent_click_test

WHY THIS EXISTS: a real DevTools Network capture (done by hand, not by
this script) confirmed the "Задания" left-rail icon inside the real
Teams shell IS the page whose iframe calls the three real $filter
queries agents/teams/agent.py already assumes (against
/edu/me/work). Every attempt to reach that data by loading a URL
directly (bare, networkidle-primed, or the class-scoped
assignments.edu.cloud.microsoft/classes/<id>/list page) failed with
401 or fired no API call at all - because the Assignments iframe only
gets its auth context via a live postMessage handshake with the real
Teams shell, which only happens when the shell itself opens that
iframe (i.e. when a real click happens inside teams.cloud.microsoft).

So instead of trying to replicate the URL or the token, this script
does what a human does: it drives an actual click on the "Задания"
icon in Teams' left rail, headless, and listens for the SAME network
responses scripts/teams_capture_assignments.py already knows how to
capture - proving (or disproving) that a real UI click, not a bare
navigation, is what TeamsAgent actually needs to do.
"""
from __future__ import annotations

import asyncio
import json

from agents.teams.auth import TEAMS_URL, TeamsSession
from config import load_config

_INTERESTING_SUBSTRINGS = ("edu.cloud.microsoft",)


async def main() -> None:
    config = load_config()
    profile_dir = getattr(config, "teams_profile_path", "data/teams_browser_profile")
    session = TeamsSession(profile_dir)

    print(f"Using browser profile at: {profile_dir}")
    context = await session._ensure_context(headless=True)
    page = await context.new_page()

    captured: list[dict] = []

    async def _on_response(response) -> None:
        lowered = response.url.lower()
        if not any(s in lowered for s in _INTERESTING_SUBSTRINGS):
            return
        try:
            body = await response.json()
            item_count = len(body.get("value", [])) if isinstance(body, dict) else None
        except Exception:  # noqa: BLE001 - not every response is JSON
            item_count = None
        captured.append({"status": response.status, "url": response.url, "items": item_count})
        print(f"  [captured] {response.status} items={item_count} {response.url[:140]}")

    page.on("response", lambda r: asyncio.create_task(_on_response(r)))

    print("Loading Teams shell (headless)...")
    await page.goto(TEAMS_URL, wait_until="networkidle", timeout=30_000)

    # Try a few selector strategies for the "Задания"/"Assignments"
    # left-rail icon, since the real label/role wasn't confirmed by a
    # real capture yet - this prints which one (if any) actually found
    # and clicked something, so the real selector can be pinned down
    # for agent.py's redesign instead of guessed again later.
    candidates = [
        ("get_by_role link 'Задания'", lambda: page.get_by_role("link", name="Задания")),
        ("get_by_role button 'Задания'", lambda: page.get_by_role("button", name="Задания")),
        ("get_by_text 'Задания' (exact)", lambda: page.get_by_text("Задания", exact=True)),
        ("get_by_role link 'Assignments'", lambda: page.get_by_role("link", name="Assignments")),
        ("get_by_role button 'Assignments'", lambda: page.get_by_role("button", name="Assignments")),
    ]

    clicked_with = None
    for label, locator_fn in candidates:
        try:
            locator = locator_fn().first
            await locator.wait_for(state="visible", timeout=5_000)
            print(f"Trying to click via: {label}")
            await locator.click(timeout=5_000)
            clicked_with = label
            break
        except Exception as exc:  # noqa: BLE001 - just trying the next candidate
            print(f"  ({label} didn't work: {type(exc).__name__})")
            continue

    if clicked_with is None:
        print(
            "\nVERDICT: none of the tried selectors could find/click a 'Задания' element. "
            "This needs a real selector from you instead of a guess - see the instructions "
            "printed at the end of this run."
        )
        await session.close()
        return

    print(f"\nClicked via: {clicked_with!r}. Waiting for the page to settle and fire its own requests...")
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except Exception:  # noqa: BLE001 - best-effort extra settle time, not fatal
        pass
    await page.wait_for_timeout(4_000)

    # Debug aid: what did the click actually land on? Top-level URL stays
    # static (SPA), but a screenshot + frame list shows the real result
    # regardless, same as teams_agent_navigation_test.py already does.
    print(f"\nTop-level page URL after click: {page.url}")
    print("Frames on this page:")
    for frame in page.frames:
        if frame.url:
            print(f"  frame: {frame.url}")
    screenshot_path = "teams_capture/click_test_result.png"
    import pathlib
    pathlib.Path("teams_capture").mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=screenshot_path, full_page=True)
    print(f"Saved a screenshot of the result to: {screenshot_path} - open it to see what actually got clicked.")

    work_responses_so_far = [c for c in captured if "/edu/me/work" in c["url"]]
    print(f"\n{len(captured)} matching response(s) captured so far "
          f"({len(work_responses_so_far)} against /edu/me/work).")

    if not captured:
        print(
            "VERDICT: the click succeeded but no matching response was seen in the wait "
            "window. Either the click landed on the wrong element, or more time/a different "
            "wait strategy is needed."
        )
        await session.close()
        return

    # The dashboard has several tabs (Предстоящие/Готово к оценке/
    # Просрочено/Возвращено/Черновики per the screenshot you sent) - only
    # the default one fires on load. Click through the student-relevant
    # ones (skipping "Черновики"/Drafts, which is teacher-only content
    # creation) to see whether each one fires its OWN /edu/me/work call
    # with a different $filter, the way agents/teams/agent.py's original
    # three-filter design assumed.
    tab_labels = ["Предстоящие", "Просрочено", "Возвращено", "Готово к оценке"]
    for tab_label in tab_labels:
        try:
            tab = page.get_by_role("tab", name=tab_label).first
            await tab.wait_for(state="visible", timeout=3_000)
        except Exception:
            try:
                tab = page.get_by_text(tab_label, exact=True).first
                await tab.wait_for(state="visible", timeout=3_000)
            except Exception as exc:  # noqa: BLE001 - just report and move on
                print(f"\n  Could not find a clickable '{tab_label}' tab ({type(exc).__name__}) - skipping.")
                continue
        before_count = len(captured)
        try:
            print(f"\n  Clicking tab: {tab_label!r}")
            await tab.click(timeout=5_000)
            await page.wait_for_timeout(3_000)
        except Exception as exc:  # noqa: BLE001 - report and move on to the next tab
            print(f"    Click failed ({type(exc).__name__}) - skipping.")
            continue
        new_responses = captured[before_count:]
        if not new_responses:
            print(f"    No new matching response after clicking {tab_label!r}.")

    work_responses = [c for c in captured if "/edu/me/work" in c["url"]]
    print(f"\nTotal /edu/me/work responses captured across all tabs: {len(work_responses)}")
    for r in work_responses:
        print(f"  {r['status']} items={r['items']}  {r['url'][:160]}")

    statuses = {c["status"] for c in captured}
    print(
        f"\nVERDICT: a real UI click (+ tab clicks) triggered {len(work_responses)} "
        f"/edu/me/work response(s), HTTP status(es) seen overall: {statuses}. "
        "This is the real, verified way to get the data: drive the Assignments dashboard "
        "click, then the tab clicks, and parse the response BODIES captured here directly - "
        "no separate context.request call needed at all. agents/teams/agent.py should be "
        "redesigned around this, using this same click sequence and response-capture pattern."
    )

    await session.close()


if __name__ == "__main__":
    asyncio.run(main())
