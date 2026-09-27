"""One-off diagnostic: does Chromium's saved-password autofill for
data/sso_browser_profile even fire when HEADED (a real, visible window),
separate from whether it fires HEADLESS (scripts/sso_test_autofill_login.py
just showed it does NOT within 20s headless - that alone doesn't say
whether nothing is saved at all, or headless specifically suppresses it -
a real, documented difference in some Chromium builds/modes).

Deliberately does NOT call is_logged_in() or click anything - just opens
the login page in a single headed context, watches whether the password
field's length becomes > 0 within ~10s (checked entirely inside the
page's own JS, exactly like login_via_autofill() - the value itself
never reaches this Python process), and reports that. No context
switching, so none of the headed->headless close/reopen risk applies
here at all.

You'll also SEE the window open - if the fields visibly fill in front
of you (like your own Chrome did earlier), that confirms it directly.

Run it yourself:

    python -m scripts.sso_diagnose_headed_autofill
"""
from __future__ import annotations

import asyncio
import sys

from playwright.async_api import async_playwright

from agents.sso.auth import SSO_URL

PROFILE_DIR = "data/sso_browser_profile"

sys.stdout.reconfigure(line_buffering=True)


async def main() -> None:
    print(f"Opening a REAL, visible window at {SSO_URL} using {PROFILE_DIR}...")
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 800},
            ignore_https_errors=True,
        )
        page = await context.new_page()
        await page.goto(SSO_URL, wait_until="domcontentloaded", timeout=30_000)

        print("Watching the password field for up to 10s (length only, never the value)...")
        filled = True
        try:
            await page.wait_for_function(
                "document.querySelector('input[type=password]')?.value?.length > 0",
                timeout=10_000,
            )
        except Exception:  # noqa: BLE001 - a timeout here just means "no", handled right below
            filled = False

        print(f"\nPassword field autofilled within 10s (headed): {filled}")
        if filled:
            print("Leaving the window open for 5s so you can see it for yourself...")
            await page.wait_for_timeout(5_000)

        await page.close()
        await context.close()

    print(
        "\nDone. Send back the True/False above - if it's True here but was False in "
        "scripts/sso_test_autofill_login.py's headless run, autofill is real but headless-"
        "suppressed in this Chromium build. If it's False here too, nothing is actually "
        "saved for this profile yet, whatever saved earlier was in a different browser/profile."
    )


if __name__ == "__main__":
    asyncio.run(main())
