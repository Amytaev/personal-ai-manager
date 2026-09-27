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


class SsoAutofillLoginFailed(RuntimeError):
    """Raised by login_via_autofill() when the password field never got
    filled in time, or IsAuthenticated still says false after clicking
    the login button - see that method's docstring for what each of
    those actually means."""


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
            # REAL bug found live (2026-09-27, first run.py run of
            # SsoAgent): context.request.get() against
            # api.satbayev.university failed with "unable to verify the
            # first certificate" - while page.goto() to
            # stud.satbayev.university, moments earlier, in the SAME
            # profile, raised nothing. Not a guess: Playwright's
            # context.request goes through its own (Node-based) network
            # stack, which - unlike a real Chromium page navigation -
            # does not automatically fetch a missing intermediate
            # certificate via AIA chasing. Chromium's page-rendering
            # engine does that fetch-and-cache transparently, which is
            # exactly why the identical (likely incomplete server-side)
            # chain never surfaced as a problem for page.goto(). Since
            # agents/sso/agent.py deliberately fetches everything through
            # context.request (see its docstring for why), this would
            # otherwise NeedsReauth-fail on every single run regardless
            # of session validity. Scoped to only this SSO profile - the
            # domain here is the user's own, known university API, not
            # an unknown/attacker-controlled one, and this stays
            # read-only automation either way.
            ignore_https_errors=True,
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

    async def login_via_autofill(self, headless: bool = True, timeout_seconds: int = 20) -> bool:
        """Re-authenticates using ONLY Chromium's own saved-password
        autofill for this profile (Desae confirmed directly, 2026-09-27:
        he saved his SSO password via the browser's own "Save password?"
        prompt while logging in through this exact profile) - this
        method never reads, types, or otherwise handles the actual
        password itself. It:

          1. Opens the SSO login page.
          2. Waits for Chromium to autofill the password field - checked
             with page.wait_for_function(), which runs a length check
             INSIDE the page's own JS context and only ever returns a
             boolean/timeout back to this Python process. The actual
             field value never crosses into this code, is never read via
             input_value(), and is never logged - the whole point of
             relying on the browser's own autofill instead of an
             SSO_PASSWORD env var.
          3. Clicks the "ВОЙТИ" submit button (Chromium autofills BOTH
             fields together for this site in every real test so far -
             the login field is filled by the same event as the
             password field, so waiting on the password field alone is
             sufficient).
          4. Confirms real success the same authoritative way everything
             else in this module does - is_logged_in()'s real
             Auth/IsAuthenticated call, not a URL/DOM guess.

        Returns True on confirmed success. Raises SsoAutofillLoginFailed
        if autofill never happened within timeout_seconds (most likely:
        the password was never saved for this profile, or Chromium's
        local password manager isn't enabled in this build) or if
        IsAuthenticated still says false after clicking (most likely:
        the saved credentials are stale - the real password changed).

        HONEST LIMITATION: relies entirely on Chromium's own autofill
        heuristics for this specific login form (single saved credential
        -> autofilled without needing an explicit suggestion-dropdown
        click, confirmed by direct observation, not assumed) - a form
        that starts requiring a captcha, an SMS code, or 2FA will make
        this fail exactly like a truly expired session would, and there
        is no programmatic way around that; a human still has to log in
        by hand at that point (scripts/sso_login_setup.py).

        headless defaults to True (not False like login_interactively())
        on purpose: is_logged_in() below always reuses a headless
        context, and calling it right after a headED login here would
        force _ensure_context() to close-and-reopen headless mid-call -
        exactly the close/reopen sequence that, earlier the same day
        this method was written, needed a 3s pause to reliably avoid a
        cookie-flush race (see scripts/sso_login_setup.py's history).
        Staying headless throughout sidesteps that risk entirely rather
        than re-relying on a timing workaround.
        """
        context = await self._ensure_context(headless=headless)
        page = await context.new_page()
        try:
            await page.goto(SSO_URL, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_function(
                    "document.querySelector('input[type=password]')"
                    "?.value?.length > 0",
                    timeout=timeout_seconds * 1000,
                )
            except Exception as exc:  # noqa: BLE001 - timeout or selector miss, both mean "autofill didn't happen"
                raise SsoAutofillLoginFailed(
                    "Password field was never autofilled - either no password is saved for "
                    "this profile, or Chromium's local password manager didn't fill it in time."
                ) from exc

            await page.locator("button:has-text('войти')").first.click(timeout=5_000)

            if not await self.is_logged_in():
                raise SsoAutofillLoginFailed(
                    "Clicked the login button, but Auth/IsAuthenticated still says false - "
                    "the saved credentials are most likely stale (password changed)."
                )
            logger.info("SSO autofill login succeeded (profile: %s).", self.profile_dir)
            return True
        finally:
            await page.close()
