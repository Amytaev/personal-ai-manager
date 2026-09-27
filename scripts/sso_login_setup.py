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
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

SSO_URL = "https://sso.satbayev.university/"
PROFILE_DIR = "data/sso_browser_profile"

# Chromium's on-disk cookie store moved from Default/Cookies to
# Default/Network/Cookies in newer versions (network-service split) -
# try both rather than guess which one Playwright's bundled Chromium
# uses.
_COOKIE_DB_CANDIDATES = ("Default/Network/Cookies", "Default/Cookies")
_COOKIE_NAMES_TO_CHECK = ("kaznitu.auth.cookie", "__RequestVerificationToken", "_culture")

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


def _inspect_cookie_db_on_disk(profile_dir: str) -> None:
    """Reads Chromium's own cookie SQLite file directly, bypassing
    Playwright's API entirely - the decisive check for "did the cookie
    actually make it to disk at all", separate from "does a freshly
    launched context load/send it". kaznitu.auth.cookie showed up with a
    real future expiry (not -1/SESSION) in the headed context.cookies()
    read, which rules out the plain session-cookie theory - so the next
    question is whether it was ever written to disk in the first place,
    or written but not loaded/sent back by the very next launch. Prints
    is_persistent/has_expires/expires_utc metadata only - the row's
    value/encrypted_value columns are never read or printed, those are
    live credential material."""
    db_path = next(
        (p for p in (Path(profile_dir) / c for c in _COOKIE_DB_CANDIDATES) if p.exists()),
        None,
    )
    print("\nInspecting the on-disk cookie store directly (bypassing Playwright's API)...")
    if db_path is None:
        tried = [str(Path(profile_dir) / c) for c in _COOKIE_DB_CANDIDATES]
        print(f"  No cookie SQLite file found - tried: {tried}")
        return
    print(f"  Found: {db_path}")
    try:
        # sqlite3's URI mode needs an ABSOLUTE file: URI - a relative one
        # (what db_path.as_posix() gives here, since PROFILE_DIR itself
        # is relative) reliably fails to open on Windows with exactly
        # "unable to open database file", which is what a first real run
        # of this hit - Path.resolve().as_uri() is the fix.
        uri = db_path.resolve().as_uri() + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5)
        conn.row_factory = sqlite3.Row
        placeholders = ",".join("?" for _ in _COOKIE_NAMES_TO_CHECK)
        rows = conn.execute(
            f"SELECT host_key, name, is_persistent, has_expires, expires_utc "
            f"FROM cookies WHERE name IN ({placeholders})",
            _COOKIE_NAMES_TO_CHECK,
        ).fetchall()
        conn.close()
    except Exception as exc:  # noqa: BLE001 - a diagnostic read failing shouldn't crash the whole script
        print(f"  Could not read the cookie DB directly: {exc!r}")
        return
    if not rows:
        print("  No matching rows in the on-disk store at all (not even _culture).")
        return
    for row in rows:
        print(
            f"  - {row['name']} @ {row['host_key']} - is_persistent={row['is_persistent']} "
            f"has_expires={row['has_expires']} expires_utc_raw={row['expires_utc']}"
        )


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

        _inspect_cookie_db_on_disk(PROFILE_DIR)

        # A short pause here rules out a cheap-but-real alternative
        # explanation: Chromium/the OS not having fully released its
        # file lock on the just-closed profile's cookie DB before the
        # very next launch tries to read it (more of a risk on Windows
        # than Linux, where file locking is stricter).
        print("\nWaiting 3s before reopening, to rule out a file-lock/timing race...")
        await asyncio.sleep(3)

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
        "\nDone. Send back everything above: the final URL after manual login, the "
        "on-disk cookie store rows, the headless final URL, whether it matches, and the "
        "cookie domain/name/expiry lists (no values) - together they show whether the "
        "cookie ever reaches disk at all, or reaches disk but isn't loaded/sent back."
    )


if __name__ == "__main__":
    asyncio.run(main())
