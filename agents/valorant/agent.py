"""VALORANT Agent (Phase 6) - reads the daily store from Stack B.

Drives a headless Playwright page against the real saved Stack B
session (agents/valorant/auth.py) and captures the "storefront"
Livewire component's own POST /livewire/update response - the same
"observe what the page's own JS already fetches, don't call an
undocumented endpoint blind" discipline TeamsAgent already uses for
Teams' Assignments dashboard (see agents/teams/agent.py's docstring),
for the same reason: Stack B has no documented JSON API either (see
agents/valorant/parser.py's docstring for exactly what was verified).

Two distinct failure modes this agent tells apart on purpose, because
they need different responses from the human on the other end:

- NeedsReauth: Stack B redirected the request to its own /login page -
  the saved session has expired. Nothing about the HTML changed; the
  fix is "log back in" (scripts/valorant_login_setup.py), not "go
  debug the parser". run() reports this as AgentResult.data
  {"needs_reauth": True} specifically so run.py can send a distinct,
  actionable Telegram notification instead of a generic failure.
- VALORANT_STORE_PARSE_ERROR: the session IS valid (no login redirect)
  but agents/valorant/parser.py found zero items in the response HTML -
  Stack B's markup has likely changed. This should NOT be silently
  treated as "empty store today" (a real VALORANT store is never
  actually empty) - it's surfaced as a distinct error string so it's
  obviously not the same problem as a stale session.
"""
from __future__ import annotations

import asyncio
import json
import logging

from agents.base import AgentResult, AgentStatus, BaseAgent
from agents.valorant.auth import LOGIN_URL_HINT, STORE_URL, ValorantSession
from agents.valorant.parser import parse_reset_time_left, parse_storefront_items
from storage.database import Database

logger = logging.getLogger(__name__)

# Matched as a cheap substring check against the raw (still-serialized)
# snapshot string of each captured component, before ever bothering to
# json.loads() it - real traffic (Phase 6.1's capture) shows
# /livewire/update fires constantly for OTHER components too
# (login-form while on the login page, live-comments in the background
# on the store page itself), so this avoids doing real parsing work on
# every one of those just to find out it wasn't the one we want.
_LIVEWIRE_UPDATE_PATH = "/livewire/update"
_STOREFRONT_COMPONENT_MARKER = '"name":"storefront"'

# How long to wait for the storefront component's own response after
# opening the store page before giving up - mirrors TeamsAgent's
# _RESPONSE_WAIT_TIMEOUT_MS/_RESPONSE_POLL_INTERVAL_MS (agents/teams/
# agent.py), same reasoning: long enough for a real page load over a
# home connection, short enough not to hang the whole scheduled cycle
# if something's actually wrong.
_RESPONSE_WAIT_TIMEOUT_MS = 15_000
_RESPONSE_POLL_INTERVAL_MS = 250


class NeedsReauth(RuntimeError):
    """Raised when Stack B redirected to its own login page - the saved
    session has expired and a human needs to log back in by hand
    (scripts/valorant_login_setup.py). Deliberately a distinct exception
    from a generic RuntimeError so run() can tell this apart from an
    actual parse/markup problem."""


class ValorantAgent(BaseAgent):
    def __init__(
        self,
        profile_dir: str,
        db: Database,
        session: ValorantSession | None = None,
    ):
        super().__init__(name="valorant")
        # A pre-built ValorantSession can be injected (tests, or a
        # caller that wants to share one session) - otherwise this agent
        # owns and closes its own, same pattern as TeamsAgent.
        self.session = session or ValorantSession(profile_dir)
        self._owns_session = session is None
        self.db = db

    async def aclose(self) -> None:
        if self._owns_session:
            await self.session.close()

    async def _fetch_storefront(self) -> tuple[str, dict]:
        """Opens the store page headless, captures the "storefront"
        Livewire component's own response, and returns its
        (rendered_html, snapshot_data) - the two pieces
        agents/valorant/parser.py's two functions each need. Raises
        NeedsReauth if the session has expired, or a plain RuntimeError
        if the session looks valid but no matching response ever
        arrived (page layout/behavior changed)."""
        context = await self.session._ensure_context(headless=True)  # noqa: SLF001 - see auth.py
        page = await context.new_page()

        captured: list[dict] = []

        async def _on_response(response) -> None:
            if _LIVEWIRE_UPDATE_PATH not in response.url:
                return
            try:
                body = await response.json()
            except Exception:  # noqa: BLE001 - not every response is JSON, and a bad
                # body here shouldn't crash the listener and silently stop
                # capturing every response after it
                return
            components = body.get("components") if isinstance(body, dict) else None
            if not isinstance(components, list):
                return
            for component in components:
                snapshot_raw = component.get("snapshot")
                if isinstance(snapshot_raw, str) and _STOREFRONT_COMPONENT_MARKER in snapshot_raw:
                    captured.append(component)

        page.on("response", lambda r: asyncio.create_task(_on_response(r)))

        try:
            await page.goto(STORE_URL, wait_until="domcontentloaded", timeout=30_000)

            if LOGIN_URL_HINT in page.url:
                raise NeedsReauth(
                    f"Stack B redirected to its login page ({page.url}) - the saved session "
                    "has expired. Run scripts/valorant_login_setup.py to log back in."
                )

            elapsed_ms = 0
            while not captured and elapsed_ms < _RESPONSE_WAIT_TIMEOUT_MS:
                await asyncio.sleep(_RESPONSE_POLL_INTERVAL_MS / 1000)
                elapsed_ms += _RESPONSE_POLL_INTERVAL_MS

            if not captured:
                raise RuntimeError(
                    f"No storefront Livewire response was seen within "
                    f"{_RESPONSE_WAIT_TIMEOUT_MS}ms of opening {STORE_URL} - session looked "
                    "valid (no /login redirect), so Stack B's page may have changed how it "
                    "loads the store."
                )
        finally:
            await page.close()

        # If more than one storefront response arrived (unlikely in one
        # short page load, but not impossible), the last one wins - the
        # freshest render of the same component, same "last write wins"
        # reasoning TeamsAgent's by_id merge already uses.
        component = captured[-1]
        html = component.get("effects", {}).get("html", "") or ""
        try:
            snapshot = json.loads(component.get("snapshot") or "{}")
            snapshot_data = snapshot.get("data") or {}
        except (json.JSONDecodeError, TypeError):
            snapshot_data = {}
        return html, snapshot_data

    async def run(self) -> AgentResult:
        try:
            html, snapshot_data = await self._fetch_storefront()
        except NeedsReauth as exc:
            return AgentResult(
                agent=self.name,
                status=AgentStatus.FAILING,
                error=str(exc),
                data={"needs_reauth": True},
            )
        except Exception as exc:  # noqa: BLE001 - expected failure mode (network, timeout, UI change)
            return AgentResult(agent=self.name, status=AgentStatus.FAILING, error=f"{type(exc).__name__}: {exc}")

        items = parse_storefront_items(html)
        if not items:
            return AgentResult(
                agent=self.name,
                status=AgentStatus.FAILING,
                error=(
                    "VALORANT_STORE_PARSE_ERROR: session looked valid but zero items were "
                    "found in the storefront response - Stack B likely changed its markup, "
                    "see agents/valorant/parser.py."
                ),
                data={"needs_reauth": False},
            )

        reset_in = parse_reset_time_left(snapshot_data)
        self.db.save_valorant_store(items, reset_in=reset_in)

        logger.info("VALORANT agent: %s item(s) in today's store (reset in %s)", len(items), reset_in)
        return AgentResult(
            agent=self.name,
            status=AgentStatus.WORKING,
            data={"items": items, "reset_in": reset_in, "needs_reauth": False},
        )
