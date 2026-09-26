"""Persistent Teams browser session (TZ v4 §5.3/§9 - Interactive Authentication).

The app never sees the user's Microsoft password. A persistent
Playwright profile (a real Chromium user-data-dir on disk) holds the
login session, exactly like an ordinary browser remembers a signed-in
account. The first time - or whenever the session has expired - the
user logs in by hand, including any MFA/CAPTCHA/Conditional Access the
tenant throws at them; this module only opens the window and waits for
a logged-in state, it never touches the password field itself.

No credential extraction, no autofill, no anti-detection tricks - see
the security discussion in this project's history for why: any of
those would put a plaintext password back in the app's process, which
is exactly what this whole design exists to avoid.

IMPORTANT / HONEST LIMITATION: the "logged in" detection below
(LOGGED_IN_URL_HINT) is a best-effort guess based on Teams' commonly
seen web client URL shape - it has NOT been verified against a real
Satbayev University Microsoft tenant, because this code was written in
a sandbox with no network access to teams.microsoft.com. The
scripts/teams_login_setup.py script (run locally, once) reports the
*actual* post-login URL it sees, so this constant can be corrected
with real data instead of a guess.
"""
from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright

logger = logging.getLogger(__name__)

TEAMS_URL = "https://teams.microsoft.com/v2/"
# See the module docstring: unverified guess, to be corrected with real
# data from scripts/teams_login_setup.py.
LOGGED_IN_URL_HINT = "teams.microsoft.com/v2/"


class TeamsLoginTimeout(RuntimeError):
    """Raised by login_interactively() when the user didn't finish
    logging in within the given timeout."""


class TeamsSession:
    """Owns one persistent browser profile for Teams.

    Only one Chromium process may use a given profile_dir at a time,
    so this class only ever keeps a single BrowserContext open,
    recreating it if the caller asks for a different headless mode.
    """

    def __init__(self, profile_dir: str | Path):
        self.profile_dir = Path(profile_dir)
        self._playwright = None
        self._context: BrowserContext | None = None
        self._context_headless: bool | None = None

    async def _ensure_context(self, headless: bool) -> BrowserContext:
        if self._context is not None and self._context_headless == headless:
            return self._context
        if self._context is not None:
            await self._context.close()
            self._context = None

        if self._playwright is None:
            self._playwright = await async_playwright().start()

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=headless,
            viewport={"width": 1280, "height": 800},
        )
        self._context_headless = headless
        return self._context

    async def close(self) -> None:
        if self._context is not None:
            await self._context.close()
            self._context = None
            self._context_headless = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def is_logged_in(self) -> bool:
        """Best-effort headless check: open Teams, see whether the
        saved session is still valid. Used by the scheduled agent
        before every data-fetch cycle - a plain automated run, not an
        evasion technique (TZ v4 §3.4): same persistent profile, same
        cookies a normal signed-in browser would have.
        """
        context = await self._ensure_context(headless=True)
        page = await context.new_page()
        try:
            await page.goto(TEAMS_URL, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_url(f"**{LOGGED_IN_URL_HINT}**", timeout=15_000)
                return True
            except Exception:  # noqa: BLE001 - Playwright's own TimeoutError, treated as "not logged in"
                return False
        finally:
            await page.close()

    async def login_interactively(self, timeout_seconds: int = 300) -> str:
        """Opens a REAL, visible browser window and waits for the user
        to sign in by hand. Returns the URL Playwright saw once it
        detected a logged-in state (useful for correcting
        LOGGED_IN_URL_HINT with real data). Raises TeamsLoginTimeout if
        the user doesn't finish within timeout_seconds.
        """
        context = await self._ensure_context(headless=False)
        page = await context.new_page()
        logger.info(
            "Opening Teams for interactive login - please sign in in the window that opened "
            "(timeout: %ss).",
            timeout_seconds,
        )
        await page.goto(TEAMS_URL, wait_until="domcontentloaded")
        try:
            await page.wait_for_url(f"**{LOGGED_IN_URL_HINT}**", timeout=timeout_seconds * 1000)
        except Exception as exc:  # noqa: BLE001 - Playwright's own TimeoutError
            raise TeamsLoginTimeout(
                f"Login was not completed within {timeout_seconds}s (last URL: {page.url})"
            ) from exc
        final_url = page.url
        logger.info("Interactive Teams login detected as successful (URL: %s).", final_url)
        await page.close()
        return final_url
