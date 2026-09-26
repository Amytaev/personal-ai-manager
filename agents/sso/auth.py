"""Persistent SSO (sso.satbayev.university / stud.satbayev.university)
browser session (Этап A/B, SSO Agent).

Same discipline as agents/teams/auth.py and agents/valorant/auth.py: this
app never sees the user's SSO password. A persistent Playwright profile
(data/sso_browser_profile/ - completely separate from
data/teams_browser_profile/ and data/valorant_browser_profile/, never
mixed) holds the login session. scripts/sso_login_setup.py is the one
place a human logs in by hand; this module only reuses that profile.

WHY THE "logged in" CHECK IS AN API CALL, NOT A PAGE NAVIGATION - unlike
Teams (agents/teams/auth.py's LOGGED_IN_URL_HINT, a guessed page URL) and
VALORANT (agents/valorant/auth.py's LOGIN_URL_HINT, a real login-redirect
URL), the real research capture for this system
(scripts/sso_capture_data.py's Этап A run, 2026-09-27) found a clean,
purpose-built JSON endpoint for exactly this question:

    GET https://api.satbayev.university/api/Auth/IsAuthenticated -> true

Confirmed for real in that capture (fired automatically by the Angular
app on every page load) and directly authoritative - no URL-comparison
guessing needed, no risk of the same wait_for_url()-against-itself no-op
bug that cost real debugging time on Teams (see agents/teams/auth.py's
docstring). It's called through ``context.request`` (Playwright's own
cookie-authenticated HTTP client, sharing the persistent profile's
cookie jar) rather than through a ``page``, since it's a plain JSON GET
with no UI behind it at all.

HONEST LIMITATION: the capture that found this endpoint was taken during
a session that stayed valid the whole time - there is no REAL observed
example yet of what IsAuthenticated (or a request made after it actually
expires) looks like. This treats any non-200 response, or a body other
than the literal JSON ``true``, as "not logged in" - the safe default
until a real expired-session capture confirms the exact shape.
"""
from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright

logger = logging.getLogger(__name__)

SSO_URL = "https://sso.satbayev.university/"
API_BASE = "https://api.satbayev.university"
STUD_BASE = "https://stud.satbayev.university"

IS_AUTHENTICATED_URL = f"{API_BASE}/api/Auth/IsAuthenticated"
# Used only as a real page to open during interactive login / as a light
# "look like a normal page load" navigation before the API calls below -
# confirmed reachable and rendering real personal content headless by
# scripts/sso_check_portal_access.py.
SCHEDULE_PAGE_URL = f"{STUD_BASE}/#!/82/student-schedule"


class SsoLoginTimeout(RuntimeError):
    """Raised by login_interactively() when the user didn't finish
    logging in within the given timeout."""


class SsoSession:
    """Owns one persistent browser profile for the SSO/student portal.

    Mirrors agents/teams/auth.py's TeamsSession and
    agents/valorant/auth.py's ValorantSession - only one Chromium process
    may use a given profile_dir at a time, so this keeps a single
    BrowserContext open, recreating it if the caller asks for a
    different headless mode.
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
        """Headless check via the real Auth/IsAuthenticated endpoint (see
        this module's docstring) - not a URL-comparison guess. Used by
        SsoAgent before every fetch cycle: a plain automated read against
        the same persistent profile a signed-in browser would use, not
        an evasion technique (same TZ v4 §3.4 discipline as Teams/
        VALORANT).

        Any non-200 response, or a body that isn't the literal JSON
        ``true``, is treated as "not logged in" - see the docstring's
        honest-limitation note on why this is a safe default rather than
        a confirmed expired-session shape.
        """
        context = await self._ensure_context(headless=True)
        response = await context.request.get(IS_AUTHENTICATED_URL, timeout=15_000)
        if not response.ok:
            return False
        try:
            body = await response.json()
        except Exception:  # noqa: BLE001 - a non-JSON body here is not "logged in"
            return False
        return body is True

    async def login_interactively(self, timeout_seconds: int = 300) -> str:
        """Opens a REAL, visible browser window at the SSO login page and
        waits for the user to sign in by hand. Returns the URL Playwright
        saw once the session-authenticated check above first started
        reporting True. Raises SsoLoginTimeout if the user doesn't finish
        within timeout_seconds.

        scripts/sso_login_setup.py remains the primary, already-verified
        way to do this by hand - this method exists so SsoAgent/a future
        Auth Checker Agent has the same programmatic capability
        TeamsSession/ValorantSession already offer, for consistency.
        """
        context = await self._ensure_context(headless=False)
        page = await context.new_page()
        logger.info(
            "Opening the SSO login page - please sign in in the window that opened "
            "(timeout: %ss).",
            timeout_seconds,
        )
        await page.goto(SSO_URL, wait_until="domcontentloaded")

        elapsed_ms = 0
        poll_interval_ms = 1_000
        while elapsed_ms < timeout_seconds * 1000:
            if await self.is_logged_in():
                final_url = page.url
                logger.info("Interactive SSO login detected as successful (URL: %s).", final_url)
                await page.close()
                return final_url
            await page.wait_for_timeout(poll_interval_ms)
            elapsed_ms += poll_interval_ms

        last_url = page.url
        await page.close()
        raise SsoLoginTimeout(
            f"Login was not completed within {timeout_seconds}s (last URL: {last_url})"
        )
