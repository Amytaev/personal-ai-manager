"""One-off validation script for SsoSession.login_via_autofill()
(agents/sso/auth.py) - Desae confirmed directly (2026-09-27) that he
clicked "Save password?" while logging in through data/sso_browser_profile
via scripts/sso_login_setup.py, so Chromium's own local password manager
now has real saved credentials for this profile. This script proves,
with real data, whether autofill + a button click can re-authenticate a
session from a LOGGED-OUT state - not just confirm a still-valid cookie
(the current session is likely still valid for another chunk of its
~1h lifetime, which would make a plain before/after check meaningless).

To get a real, honest test, this FIRST deletes only the profile's cookie
files (never the Login Data file the saved password lives in), forcing
a genuinely logged-out state, then runs login_via_autofill() to see if
it can recover from that on its own.

Never touches your password - reads/prints nothing about it, per
login_via_autofill()'s own docstring on how it avoids that entirely.

Run it yourself:

    python -m scripts.sso_test_autofill_login
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from agents.sso.auth import SsoAutofillLoginFailed, SsoSession

# A real run (2026-09-27, Windows) lost every print() between "before:
# False" and a crash - the process died abruptly enough (browser context
# gone, then an asyncio subprocess-transport deallocation error on top)
# that Windows' default block-buffered stdout never got flushed, so the
# actual failure point was invisible - only session.close()'s own
# follow-on error surfaced. Forcing line buffering here means every
# print below is on disk/screen the instant it runs, crash or not.
sys.stdout.reconfigure(line_buffering=True)

PROFILE_DIR = "data/sso_browser_profile"

# Same candidates scripts/sso_login_setup.py's on-disk inspection tries -
# deliberately NOT touching Login Data (the saved-password store) or
# Login Data-journal.
_COOKIE_FILES_TO_CLEAR = (
    "Default/Network/Cookies",
    "Default/Network/Cookies-journal",
    "Default/Cookies",
    "Default/Cookies-journal",
)


def _force_logged_out_state(profile_dir: str) -> None:
    print("Clearing this profile's cookies only (never the saved password) to force a real "
          "logged-out state to test from...")
    cleared = []
    for rel in _COOKIE_FILES_TO_CLEAR:
        path = Path(profile_dir) / rel
        if path.exists():
            path.unlink()
            cleared.append(str(path))
    print(f"  Removed: {cleared or '(nothing found - already logged out, or first run)'}")


async def main() -> None:
    _force_logged_out_state(PROFILE_DIR)

    session = SsoSession(PROFILE_DIR)
    try:
        before = await session.is_logged_in()
        print(f"\nis_logged_in() before autofill login: {before}")

        try:
            # headless=False is login_via_autofill()'s own default now -
            # confirmed live (2026-09-27) that headless suppresses
            # autofill in this profile entirely (see
            # scripts/sso_diagnose_headed_autofill.py) - passed
            # explicitly here just so it's obvious from reading this
            # script, not hidden behind a default.
            await session.login_via_autofill(headless=False)
            print("login_via_autofill(): succeeded (Auth/IsAuthenticated confirmed true).")
        except SsoAutofillLoginFailed as exc:
            print(f"login_via_autofill(): FAILED - {exc}")
        except Exception as exc:  # noqa: BLE001 - see this module's docstring/comment above: a real prior
            # run raised something OTHER than SsoAutofillLoginFailed and the real error got
            # masked - this is the fallback that guarantees it's visible instead, whatever it is.
            print(f"login_via_autofill(): UNEXPECTED {type(exc).__name__}: {exc!r}")

        after = await session.is_logged_in()
        print(f"is_logged_in() after autofill login: {after}")
    finally:
        await session.close()

    print(
        "\nDone. Send back everything above - whether it went False -> True, and any "
        "SsoAutofillLoginFailed message - that's the real, first-hand answer on whether "
        "autofill-only re-auth actually works for this profile."
    )


if __name__ == "__main__":
    asyncio.run(main())
