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
# Duplicated from agents/teams/agent.py's _ASSIGNMENTS_BUTTON_NAME (this
# project's established "small constant duplicated across a module
# boundary" call, see agents/study_manager/logic.py's docstring for the
# same reasoning) - agent.py already imports this module, so importing
# back the other way would be circular. Used by is_logged_in() below -
# see its docstring for why this module needs the same signal
# TeamsAgent's own real fetch already uses, not just a URL check.
_ASSIGNMENTS_BUTTON_NAME = "Задания"


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

        Real bug found live (2026-09-26, five live runs deep): this used
        to call page.wait_for_url(f"**{LOGGED_IN_URL_HINT}**") right after
        goto(TEAMS_URL) - but page.url is ALREADY TEAMS_URL the instant
        goto() resolves (that's the URL we just navigated to ourselves),
        so that wait_for_url() call matched immediately and returned True
        with zero real waiting, no matter what Teams' own MSAL client went
        on to do a moment later. That's exactly why this method - and the
        near-identical check in scripts/teams_diagnose_reauth.py - kept
        reporting "still logged in" mere seconds before a real TeamsAgent
        run hit a genuine, blank Microsoft sign-in form on the SAME
        profile: this check was never actually waiting for anything.
        TeamsAgent._fetch_all_assignments()'s own 15s wait for the
        "Задания" button doesn't have this flaw (it's watching for a
        DIFFERENT element to appear, not comparing a URL to itself), which
        is why it - correctly - kept reporting the session as dead while
        this method kept giving false positives.

        Fixed by waiting for the page's network activity to settle
        (giving any client-side redirect real wall-clock time to actually
        happen) before reading page.url, instead of an instant, always-
        true URL comparison.

        Second live bug, same root cause, found 2026-09-29: even with
        the networkidle fix above, this method STILL gave false
        positives - Auth Checker (and scripts/teams_login_setup.py's own
        "headless re-check", which calls this exact method) reported the
        session as fine mere seconds before a real TeamsAgent run hit the
        genuine blank Microsoft sign-in form on the SAME profile, even
        right after a fresh interactive login. Root cause: Teams is a
        heavy SPA with constant background traffic (websockets,
        telemetry), so "networkidle" (500ms of NO network activity) can
        legitimately never fire within the 15s timeout even while
        everything is working normally - and MSAL's own client-side
        redirect-to-login can take longer than that 15s to actually
        happen. agents/teams/agent.py's _fetch_all_assignments() never
        had this problem because it waits ANOTHER 15s (up to 30s total)
        for the "Задания" button to become visible before deciding -
        this method only had the first 15s, so it read page.url up to
        15s too early relative to the real agent. Fixed by waiting for
        the exact same signal TeamsAgent's own (already correct) check
        uses, giving a slow-but-real redirect the same total wall-clock
        time here that it already gets there.
        """
        context = await self._ensure_context(headless=True)
        page = await context.new_page()
        try:
            await page.goto(TEAMS_URL, wait_until="domcontentloaded", timeout=30_000)
            try:
                await page.wait_for_load_state("networkidle", timeout=15_000)
            except Exception:  # noqa: BLE001 - proceed with whatever state exists either way
                pass
            try:
                # If the assignments button shows up, the session is
                # definitely valid - no need to also check the URL.
                await page.get_by_role(
                    "button", name=_ASSIGNMENTS_BUTTON_NAME
                ).first.wait_for(state="visible", timeout=15_000)
                return True
            except Exception:  # noqa: BLE001 - Playwright's own TimeoutError, same as agent.py
                # The button never appeared even after the SAME total
                # ~30s of real wall-clock time TeamsAgent's own fetch
                # gets - page.url now decides between "genuinely logged
                # out" (redirect completed) and "logged in but the UI
                # changed" (unrelated to auth), exactly like agent.py's
                # own fallback.
                return LOGGED_IN_URL_HINT in page.url
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
