"""Auth Checker Agent (bro's ТЗ, следующий этап архитектуры после SSO
Agent/autofill).

Not a source agent itself - it fetches nothing and never touches
tasks/sso_courses/sso_schedule_entries/sso_study_materials. It is the
single place that answers one narrow question per source: "is this
source's already-collected data something we can currently trust, or
is the session behind it stale/broken right now?" - and records that
answer in storage/models.py's source_status table, so a later Checker/
Study Manager can read one unified health report (storage/database.py's
get_all_source_status()) instead of each of them re-implementing their
own auth probing.

WHY THIS SHARES SESSIONS WITH TeamsAgent/SsoAgent RATHER THAN OWNING
ITS OWN - both TeamsSession and SsoSession are explicitly documented
(agents/teams/auth.py, agents/sso/auth.py) as "only one Chromium
process may use a given profile_dir at a time". A second, independent
TeamsSession/SsoSession pointed at the SAME profile_dir would fight the
real source agent for that persistent profile's lock. So run.py
constructs one TeamsSession/SsoSession per source and passes the SAME
instances into both the real agent (TeamsAgent(session=...),
SsoAgent(session=...)) and this one - this agent only ever calls
is_logged_in() on them (a lightweight page/API read), never
launch_persistent_context() itself, so it's safe to share.

WHAT THIS DELIBERATELY DOES NOT DO (bro's ТЗ, Auth Checker step):

- Never calls login_interactively()/login_via_autofill() on either
  session - recovering a session is the owning source agent's job
  (agents/sso/agent.py already tries login_via_autofill() automatically
  before NeedsReauth); this agent only ever reports what it observes.
- Never reads tasks/sso_* tables to guess a source's health from
  "how much data is there" - an empty result set is not evidence of a
  broken auth session (a genuinely empty Teams inbox looks identical to
  a stale one from the DB's point of view), so the only signal used
  here is each session's own real is_logged_in() check.
- Never deletes or touches any other table - a source being
  temporarily AUTH_REQUIRED/ERROR must never cause its last-known-good
  data to disappear out from under /tasks, /schedule, /umkd, etc.
- Never stores a password, cookie, access token or MFA code - only a
  status string, two ISO timestamps and a short exception message (see
  storage/database.py's record_source_status()).
"""
from __future__ import annotations

import enum
import logging
from datetime import datetime, timezone
from typing import Any

from agents.base import AgentResult, AgentStatus, BaseAgent
from storage.database import Database

logger = logging.getLogger(__name__)


class SourceStatus(str, enum.Enum):
    #: is_logged_in() returned True - the session backing this source is
    #: currently valid.
    OK = "OK"
    #: is_logged_in() completed and returned False - a human (or, for
    #: SSO, SsoAgent's own automatic autofill retry) needs to log back
    #: in; this is a normal, expected state, not a bug in the check.
    AUTH_REQUIRED = "AUTH_REQUIRED"
    #: The check itself raised an unexpected exception (network failure,
    #: browser crash, a timeout inside is_logged_in()'s own navigation) -
    #: distinct from AUTH_REQUIRED because the check never got a real
    #: answer at all, so this says "don't know, and something's wrong",
    #: not "confirmed logged out".
    ERROR = "ERROR"
    #: No session object was given for this source in this run (e.g. a
    #: caller/test that only wires up one of Teams/SSO) - this source
    #: was never checked, not "checked and found broken".
    UNAVAILABLE = "UNAVAILABLE"
    #: Reserved for a status a future source-type genuinely can't
    #: classify into the four above; _check_source() below never returns
    #: this itself today, but it exists so storage/models.py's status
    #: column and any caller's switch/if-chain has a safe explicit
    #: fallback that isn't silently treated as OK or AUTH_REQUIRED.
    UNKNOWN = "UNKNOWN"


class AuthCheckerAgent(BaseAgent):
    """Checks Teams' and SSO's real session state and records it into
    storage/models.py's source_status table. See this module's docstring
    for the full set of things it deliberately does not do.

    ``teams_session``/``sso_session`` should be the SAME TeamsSession/
    SsoSession instances the real TeamsAgent/SsoAgent already own (see
    this module's docstring on why) - either may be left as None if that
    source isn't wired up for this run, which reports UNAVAILABLE for
    it rather than skipping it silently.
    """

    def __init__(
        self,
        db: Database,
        teams_session: Any | None = None,
        sso_session: Any | None = None,
    ):
        super().__init__(name="auth_checker")
        self.db = db
        # Deliberately no self._owns_session/aclose() of its own (unlike
        # TeamsAgent/SsoAgent) - this agent never opens a browser context
        # by itself (is_logged_in() does that internally on whichever
        # session it's given), so there is nothing here for it to own or
        # close. Whoever constructed these sessions (run.py, or a test)
        # remains responsible for closing them.
        self.teams_session = teams_session
        self.sso_session = sso_session

    async def _check_source(self, source: str, session: Any | None) -> tuple[SourceStatus, str | None]:
        if session is None:
            return SourceStatus.UNAVAILABLE, "no session configured for this source in this run"
        try:
            logged_in = await session.is_logged_in()
        except Exception as exc:  # noqa: BLE001 - the check itself failed; see SourceStatus.ERROR's docstring
            logger.warning(
                "Auth Checker: %s is_logged_in() raised %s: %s", source, type(exc).__name__, exc
            )
            return SourceStatus.ERROR, f"{type(exc).__name__}: {exc}"
        return (SourceStatus.OK, None) if logged_in else (SourceStatus.AUTH_REQUIRED, None)

    async def run(self) -> AgentResult:
        checked_at = datetime.now(timezone.utc).isoformat()
        report: dict[str, dict[str, str | None]] = {}

        for source, session in (("teams", self.teams_session), ("sso", self.sso_session)):
            status, error = await self._check_source(source, session)
            # Recorded per-source even when one source errors and the
            # other doesn't - a broken Teams check must never prevent a
            # healthy SSO status from being saved, and vice versa.
            self.db.record_source_status(
                source=source, status=status.value, checked_at=checked_at, error=error
            )
            report[source] = {"status": status.value, "error": error}
            logger.info("Auth Checker: %s -> %s", source, status.value)

        # ERROR is the only outcome here that means "something about the
        # check itself went wrong" - AUTH_REQUIRED/UNAVAILABLE are
        # ordinary, expected states for this agent to observe and report,
        # not a reason to mark the Auth Checker's OWN run as failing.
        any_check_errored = any(v["status"] == SourceStatus.ERROR.value for v in report.values())
        return AgentResult(
            agent=self.name,
            status=AgentStatus.DEGRADED if any_check_errored else AgentStatus.WORKING,
            data=report,
        )
