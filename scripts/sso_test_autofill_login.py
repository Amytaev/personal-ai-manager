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
from pathlib import Path

from agents.sso.auth import SsoAutofillLoginFailed, SsoSession

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
            await session.login_via_autofill(headless=True)
            print("login_via_autofill(): succeeded (Auth/IsAuthenticated confirmed true).")
        except SsoAutofillLoginFailed as exc:
            print(f"login_via_autofill(): FAILED - {exc}")

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
