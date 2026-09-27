"""One-off local RESEARCH script (Этап A of the Study Manager / SSO Agent
proposal, 2026-09-26): before building an SSO Agent at all, find out
whether sso.satbayev.university has the SAME problem Teams turned out to
have tonight - a headless Playwright session that looks perfectly valid
(cookies present, "logged in" URL) but doesn't actually survive real use,
because something on the identity-provider side treats the automated
browser as untrusted.

This does NOT build an SSO Agent. It does not know the real login flow,
the real "logged in" URL, or whether sso.satbayev.university is its own
identity system or federates through the same Microsoft/Azure AD tenant
Teams uses - all of that is unknown and deliberately not guessed at here,
per this project's standing rule (see agents/teams/agent.py's docstring
for the last time guessing instead of testing wasted a lot of time).

It NEVER touches your password. Run it yourself:

    python -m scripts.sso_login_setup

What it does, in order:
  1. Opens a REAL, visible Chromium window at sso.satbayev.university
     using a fresh persistent profile (data/sso_browser_profile/ -
     completely separate from data/teams_browser_profile/, never mixed).
  2. Waits for you to log in by hand - however many steps/redirects that
     takes (SSO -> stud.satbayev.university, or SSO -> Microsoft and
     back, or something else entirely; we don't know yet). Press Enter
     in THIS terminal once you're looking at a page that's clearly
     "inside" the student portal (schedule, УМКД, dashboard, whatever it
     actually shows you).
  3. Reports the final URL and which domains your cookies ended up on,
     plus each relevant cookie's expiry metadata - SESSION (no
     Expires/Max-Age, dropped whenever the browser process restarts) vs.
     persistent (a real future expiry timestamp) - names/domains/expiry
     only, never values, a cookie value is a live credential.
  4. Immediately reopens the SAME profile in HEADLESS mode (like a real
     scheduled agent would use) and checks, honestly this time - using
     page.wait_for_load_state("networkidle") before reading the URL,
     the same fix Teams needed tonight after wait_for_url() against a
     just-navigated-to URL turned out to be a no-op - whether that
     session still looks valid a few seconds later.

Send back everything this prints (final URLs + cookie domain/names, no
values) - that's the real, first-hand answer to "does SSO have the same
headless problem as Teams", instead of guessing before writing a single
line of SSO Agent code.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from playwright.async_api import async_playwright

SSO_URL = "https://sso.satbayev.university/"
PROFILE_DIR = "data/sso_browser_profile"

# Cookie/domain names worth reporting - broadened to anything under
# satbayev.university plus the usual Microsoft/Azure AD domains, since we
# don't yet know whether this federates through Microsoft or not.
_RELEVANT_DOMAIN_SUBSTRINGS = ("satbayev.university", "microsoftonline.com", "microsoft.com", "live.com")


def _expiry_label(cookie: dict) -> str:
    """Playwright's cookie.expires is -1 for a real session-only cookie
    (no Expires/Max-Age at all - the browser is free to drop it whenever
    it likes, including "browser process restarted", which is exactly
    what happens between this script's headed and headless contexts) or
    a Unix timestamp for a persistent one. This is metadata about the
    cookie's lifetime, not the cookie's value - safe to print."""
    expires = cookie.get("expires")
    if expires is None or expires == -1:
        return "SESSION (dropped on browser restart)"
    return f"persistent (expires {datetime.fromtimestamp(expires, tz=timezone.utc).isoformat()})"


def _print_cookie_summary(cookies: list[dict], label: str) -> None:
    relevant = [c for c in cookies if any(sub in c.get("domain", "") for sub in _RELEVANT_DOMAIN_SUBSTRINGS)]
    print(f"\n{label} - {len(relevant)} relevant cookie(s):")
    for c in relevant:
        # Name + domain + expiry metadata only - NEVER the value, that's
        # a live session credential.
        print(f"  - {c.get('name')} @ {c.get('domain')} - {_expiry_label(c)}")


async def main() -> None:
    print(f"Using a fresh, separate browser profile at: {PROFILE_DIR}")
    print(f"Opening {SSO_URL} in a real, visible window - please sign in by hand.\n")

    async with async_playwright() as pw:
        headed_context = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=False,
            viewport={"width": 1280, "height": 800},
        )
        page = await headed_context.new_page()
        await page.goto(SSO_URL, wait_until="domcontentloaded", timeout=30_000)

        input(
            "\nLog in by hand in the window that opened (this script never touches your "
            "password). Once you're on a page that clearly looks like you're inside the "
            "student portal, come back here and press Enter... "
        )

        final_url = page.url
        print(f"\nFinal URL after manual login: {final_url}")
        cookies = await headed_context.cookies()
        _print_cookie_summary(cookies, "Headed session")

        await page.close()
        await headed_context.close()

        print(
            "\nReopening the same profile headless (like a real scheduled agent would) and "
            "checking honestly - waiting for the network to settle before reading the URL..."
        )
        headless_context = await pw.chromium.launch_persistent_context(
            user_data_dir=PROFILE_DIR,
            headless=True,
            viewport={"width": 1280, "height": 800},
        )
        headless_page = await headless_context.new_page()
        await headless_page.goto(final_url, wait_until="domcontentloaded", timeout=30_000)
        try:
            await headless_page.wait_for_load_state("networkidle", timeout=15_000)
        except Exception:  # noqa: BLE001 - report whatever state exists either way
            pass

        headless_final_url = headless_page.url
        print(f"\nHeadless final URL: {headless_final_url}")
        print(f"Still on the same page as the manual login: {headless_final_url == final_url}")
        headless_cookies = await headless_context.cookies()
        _print_cookie_summary(headless_cookies, "Headless session")

        if headless_final_url != final_url:
            screenshot_path = "data/sso_diagnose_headless.png"
            await headless_page.screenshot(path=screenshot_path)
            print(f"\nURL changed under headless - saved a screenshot to {screenshot_path}.")

        await headless_page.close()
        await headless_context.close()

    print(
        "\nDone. Send back: the final URL after manual login, the headless final URL, "
        "whether they match, and the cookie domain/name lists above (no values) - that's "
        "the real first-hand answer on whether SSO has the same headless problem as Teams."
    )


if __name__ == "__main__":
    asyncio.run(main())
