"""Persistent Stack B (stackb.net) browser session (Phase 6, VALORANT Agent).

Same discipline as agents/teams/auth.py: the app never sees the user's
Riot password. Stack B's own login page (which itself hands the actual
credential entry off to Riot) handles authentication - this module only
opens a real, visible browser window there and waits for a logged-in
state. It never touches a password field, never autofills, never
reads/stores a Riot credential.

WHY stackb.net AT ALL, AND NOT RIOT DIRECTLY - real, verified findings
from this project's Phase 6 investigation, not a guess:
1. Riot's official RSO (OAuth) is gated to already-approved production
   applications - there is no self-serve way for a personal project to
   register an RSO client at all
   (support-developer.riotgames.com/hc/en-us/articles/22801670382739).
2. Even an approved RSO application wouldn't help here: Riot's own
   developer docs list "online store tracking or updates" as an
   explicitly unsupported use case, and none of the public VALORANT
   APIs (VAL-CONTENT-V1/VAL-MATCH-V1/VAL-RANKED-V1/VAL-STATUS-V1)
   expose personal store data at any access tier
   (developer.riotgames.com/docs/valorant).
3. The remaining path (the local VALORANT game client's own internal
   127.0.0.1 API, as community libraries like python-valclient use)
   needs the actual game running and logged in on the same machine -
   doesn't help "check my store from my phone", and was ruled out on
   that basis.
So Stack B - a third party that itself completes a real Riot login and
exposes the resulting per-account store through its own web UI - is the
only practically available source. Confirmed for real: Phase 6.1's
captured traffic (valorant_capture/responses.jsonl) shows real store
items coming back through exactly this page.

WHY /riot/storefront SPECIFICALLY, not /valorant/store: real captured
traffic shows /valorant/store is the page an unauthenticated visitor
lands on and immediately gets redirected away from (-> /login). The
actual authenticated per-account store - the one whose Livewire
response carries real item data - is /riot/storefront.

HONEST LIMITATION: unlike Teams' LOGGED_IN_URL_HINT (a guessed *target*
URL, later confirmed/corrected by scripts/teams_login_setup.py), the
"logged in" check here is the opposite shape and doesn't need
correcting: Stack B's own auth middleware performs a synchronous HTTP
redirect to /login for an unauthenticated request (a real 302, not an
async SPA handshake), so page.goto() finishing with LOGIN_URL_HINT
still in page.url IS the definitive "not logged in" signal, already
confirmed against real traffic in Phase 6.1 - no guessed URL to correct
later.
"""
from __future__ import annotations

import logging
from pathlib import Path

from playwright.async_api import BrowserContext, async_playwright

logger = logging.getLogger(__name__)

STORE_URL = "https://stackb.net/riot/storefront"
# Laravel's default auth middleware redirect target for an
# unauthenticated request to a protected page - confirmed against real
# captured traffic (Phase 6.1: an unauthenticated /valorant/store
# request landed on exactly this URL).
LOGIN_URL_HINT = "stackb.net/login"


class ValorantLoginTimeout(RuntimeError):
    """Raised by login_interactively() when the user didn't finish
    logging in within the given timeout."""


class ValorantSession:
    """Owns one persistent browser profile for Stack B.

    Mirrors agents/teams/auth.py's TeamsSession - only one Chromium
    process may use a given profile_dir at a time, so this keeps a
    single BrowserContext open, recreating it if the caller asks for a
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
        """Best-effort headless check: open the store page, see whether
        the saved session is still valid (i.e. Stack B did NOT redirect
        us to /login). Used by scripts/valorant_login_setup.py to
        double-check a fresh login also works headless, the same way
        Teams' equivalent already does (TZ v4 §3.4 - a plain automated
        check against the same persistent profile a signed-in browser
        would use, not an evasion technique).
        """
        context = await self._ensure_context(headless=True)
        page = await context.new_page()
        try:
            await page.goto(STORE_URL, wait_until="domcontentloaded", timeout=30_000)
            return LOGIN_URL_HINT not in page.url
        finally:
            await page.close()

    async def login_interactively(self, timeout_seconds: int = 300) -> str:
        """Opens a REAL, visible browser window at the store page and
        waits for the user to sign in by hand (Stack B's own login flow,
        which itself redirects to Riot for the actual credential entry -
        this module never sees any of it). Returns the URL Playwright
        saw once a logged-in state was detected (i.e. once the URL
        stopped containing LOGIN_URL_HINT). Raises ValorantLoginTimeout
        if the user doesn't finish within timeout_seconds.
        """
        context = await self._ensure_context(headless=False)
        page = await context.new_page()
        logger.info(
            "Opening Stack B for interactive login - please sign in (via Riot) in the "
            "window that opened (timeout: %ss).",
            timeout_seconds,
        )
        await page.goto(STORE_URL, wait_until="domcontentloaded")
        try:
            await page.wait_for_url(
                lambda url: LOGIN_URL_HINT not in url,
                timeout=timeout_seconds * 1000,
            )
        except Exception as exc:  # noqa: BLE001 - Playwright's own TimeoutError
            raise ValorantLoginTimeout(
                f"Login was not completed within {timeout_seconds}s (last URL: {page.url})"
            ) from exc
        final_url = page.url
        logger.info("Interactive Stack B login detected as successful (URL: %s).", final_url)
        await page.close()
        return final_url
