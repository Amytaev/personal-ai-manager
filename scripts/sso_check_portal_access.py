"""One-off local RESEARCH script, part 2 (Этап A, right after
scripts/sso_login_setup.py): the first script showed the SSO session's
URL and cookies survive a switch to headless mode just fine - but URL
and cookies alone don't PROVE the session is actually authenticated,
only that nothing bounced it away. This is the decisive check: reuse
the SAME profile (no fresh login - if this needs one, that's itself the
answer) and, headless, try to actually load a page that requires being
logged in - the schedule and УМКД pages the TЗ named specifically.

Never touches your password - this only navigates using the session
scripts/sso_login_setup.py already set up. Run it right after that
script, without doing anything else in between:

    python -m scripts.sso_check_portal_access

Reports, for each protected URL: the final URL it landed on, and the
first 300 characters of the page's visible text - real evidence of
whether real portal content (schedule/course names, etc.) loaded, or
whether it bounced back to a login form (same signal Teams' failure
screenshots showed - a blank/generic sign-in page is unmistakable).
"""
from __future__ import annotations

import asyncio

from playwright.async_api import async_playwright

PROFILE_DIR = "data/sso_browser_profile"

# Named directly in bro's TЗ (§5) as the two real data sources.
_URLS_TO_CHECK = {
    "schedule": "https://stud.satbayev.university/#!/82/student-schedule",
    "umkd": "https://stud.satbayev.university/#!/82/umkd-student",
}


async def _check(context, label: str, url: str) -> None:
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        try:
            await page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:  # noqa: BLE001 - report whatever state exists either way
            pass

        final_url = page.url
        try:
            text = await page.inner_text("body")
        except Exception:  # noqa: BLE001 - some pages may not have a plain body
            text = "<could not read page text>"
        snippet = " ".join(text.split())[:300]

        print(f"\n--- {label} ---")
        print(f"Requested: {url}")
        print(f"Final URL: {final_url}")
        print(f"Visible text (first 300 chars): {snippet}")
    finally:
        await page.close()


async def main() -> None:
    print(f"Reusing existing profile at: {PROFILE_DIR} (no fresh login here)\n")
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        for label, url in _URLS_TO_CHECK.items():
            await _check(context, label, url)
        await context.close()

    print(
        "\nSend both blocks back. If either shows real schedule/course/material text, "
        "the headless session is genuinely authenticated there. If either shows a login "
        "form or empty/generic content, that page needs the real logged-in flow figured "
        "out first (it may use a different auth handoff than the root SSO page)."
    )


if __name__ == "__main__":
    asyncio.run(main())
