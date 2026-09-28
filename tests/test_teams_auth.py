from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.teams.auth import TeamsLoginTimeout, TeamsSession


def _make_mock_playwright(
    page_url: str = "https://teams.microsoft.com/v2/",
    assignments_button_visible: bool = True,
):
    """Builds a fake async_playwright().start() result whose chromium
    launches a context/page that we fully control from the test.

    ``assignments_button_visible`` controls what
    ``page.get_by_role("button", name="Задания").first.wait_for(...)``
    does - real Playwright's get_by_role()/`.first` are SYNC (return a
    Locator), only `.wait_for()` on the result is async, so this mirrors
    that shape exactly rather than making the whole locator an
    AsyncMock. True (the default) resolves immediately, as if is_logged_in()
    found the real "Задания" button (live bug fix, 2026-09-29: this is
    the same signal agents/teams/agent.py's own real fetch waits for,
    added here because the old URL-only check gave false positives - see
    is_logged_in()'s docstring). False raises a TimeoutError, exactly
    like Playwright's own Locator.wait_for() does when the element never
    appears, so is_logged_in() falls through to its page.url check."""
    mock_page = AsyncMock()
    mock_page.url = page_url
    mock_page.goto = AsyncMock()
    mock_page.wait_for_url = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()
    mock_page.close = AsyncMock()

    mock_button_locator = MagicMock()
    if assignments_button_visible:
        mock_button_locator.wait_for = AsyncMock()
    else:
        mock_button_locator.wait_for = AsyncMock(side_effect=TimeoutError("button never appeared"))
    mock_role_locator = MagicMock()
    mock_role_locator.first = mock_button_locator
    mock_page.get_by_role = MagicMock(return_value=mock_role_locator)

    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.close = AsyncMock()

    mock_chromium = MagicMock()
    mock_chromium.launch_persistent_context = AsyncMock(return_value=mock_context)

    mock_playwright_instance = MagicMock()
    mock_playwright_instance.chromium = mock_chromium
    mock_playwright_instance.stop = AsyncMock()

    mock_playwright_cm = MagicMock()
    mock_playwright_cm.start = AsyncMock(return_value=mock_playwright_instance)

    return mock_playwright_cm, mock_chromium, mock_context, mock_page


@pytest.mark.asyncio
async def test_is_logged_in_true_when_url_matches_after_settling(tmp_path):
    # Real bug found live (2026-09-26): the old implementation checked
    # page.wait_for_url() against the very URL it just navigated to,
    # which resolves instantly and TRUE regardless of what happens next -
    # it never actually waited for anything. The fix waits for the
    # network to settle (page.wait_for_load_state("networkidle")) and
    # then reads page.url directly, so THAT's what these tests exercise now.
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright()

    with patch("agents.teams.auth.async_playwright", return_value=mock_cm):
        session = TeamsSession(tmp_path / "profile")
        result = await session.is_logged_in()

    assert result is True
    mock_page.wait_for_load_state.assert_awaited_once_with("networkidle", timeout=15_000)
    mock_chromium.launch_persistent_context.assert_awaited_once()
    call_kwargs = mock_chromium.launch_persistent_context.call_args.kwargs
    assert call_kwargs["headless"] is True


@pytest.mark.asyncio
async def test_is_logged_in_false_when_final_url_is_the_login_domain(tmp_path):
    # The button never appears either (a real login-domain redirect
    # means there's no Teams UI at all) - assignments_button_visible=False
    # exercises the fallback-to-url-check path added for the second live
    # bug below.
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright(
        page_url="https://login.microsoftonline.com/common/oauth2/v2.0/authorize?...",
        assignments_button_visible=False,
    )

    with patch("agents.teams.auth.async_playwright", return_value=mock_cm):
        session = TeamsSession(tmp_path / "profile")
        result = await session.is_logged_in()

    assert result is False


@pytest.mark.asyncio
async def test_is_logged_in_still_reads_url_when_networkidle_itself_times_out(tmp_path):
    # A wait_for_load_state() timeout isn't fatal - it's the best signal
    # we get about whether the page settled, and the button-visibility
    # check (then, as a last resort, page.url) still runs either way
    # rather than treating the networkidle timeout itself as failure.
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright()
    mock_page.wait_for_load_state = AsyncMock(side_effect=TimeoutError("never went idle"))

    with patch("agents.teams.auth.async_playwright", return_value=mock_cm):
        session = TeamsSession(tmp_path / "profile")
        result = await session.is_logged_in()

    assert result is True  # the assignments button (default fixture) is visible


# ===========================================================================
# Second live bug, 2026-09-29: is_logged_in() only waited ~15s total
# (networkidle) before reading page.url, while agents/teams/agent.py's
# real fetch waits ~30s total (networkidle + a further 15s for the
# "Задания" button) - so Auth Checker/scripts/teams_login_setup.py's own
# "headless re-check" kept reporting a session as fine mere seconds
# before the real agent hit a genuine, just-slow-to-redirect expired
# session on the SAME profile. Fixed by waiting for the same button the
# real agent already waits for, giving a slow real redirect the same
# total wall-clock time here that it already gets there.
# ===========================================================================


@pytest.mark.asyncio
async def test_is_logged_in_waits_for_the_assignments_button_like_the_real_agent_does(tmp_path):
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright()

    with patch("agents.teams.auth.async_playwright", return_value=mock_cm):
        session = TeamsSession(tmp_path / "profile")
        result = await session.is_logged_in()

    assert result is True
    mock_page.get_by_role.assert_called_once_with("button", name="Задания")
    button_locator = mock_page.get_by_role.return_value.first
    button_locator.wait_for.assert_awaited_once_with(state="visible", timeout=15_000)


@pytest.mark.asyncio
async def test_is_logged_in_true_via_button_even_when_url_looks_wrong(tmp_path):
    # The button appearing is authoritative - mirrors agent.py's own
    # "button visible wins" behavior, not a regression of the URL check.
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright(
        page_url="https://teams.microsoft.com/v2/some/other/path",
        assignments_button_visible=True,
    )

    with patch("agents.teams.auth.async_playwright", return_value=mock_cm):
        session = TeamsSession(tmp_path / "profile")
        result = await session.is_logged_in()

    assert result is True


@pytest.mark.asyncio
async def test_is_logged_in_true_when_button_missing_but_url_never_left_teams(tmp_path):
    # Distinct from the login-domain test above: the URL genuinely never
    # redirected (still on Teams) but the button itself didn't show up
    # for some OTHER reason - mirrors agent.py's own "session looks
    # logged in but the UI may have changed" distinction (its own
    # RuntimeError branch, separate from NeedsReauth). Not an auth
    # problem, so this still honestly reports "logged in".
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright(
        page_url="https://teams.microsoft.com/v2/",
        assignments_button_visible=False,
    )

    with patch("agents.teams.auth.async_playwright", return_value=mock_cm):
        session = TeamsSession(tmp_path / "profile")
        result = await session.is_logged_in()

    assert result is True





@pytest.mark.asyncio
async def test_login_interactively_uses_non_headless_context(tmp_path):
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright(
        page_url="https://teams.microsoft.com/v2/some-team"
    )

    with patch("agents.teams.auth.async_playwright", return_value=mock_cm):
        session = TeamsSession(tmp_path / "profile")
        final_url = await session.login_interactively(timeout_seconds=5)

    assert final_url == "https://teams.microsoft.com/v2/some-team"
    call_kwargs = mock_chromium.launch_persistent_context.call_args.kwargs
    assert call_kwargs["headless"] is False


@pytest.mark.asyncio
async def test_login_interactively_raises_teams_login_timeout(tmp_path):
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright()
    mock_page.wait_for_url = AsyncMock(side_effect=TimeoutError("still on login page"))

    with patch("agents.teams.auth.async_playwright", return_value=mock_cm):
        session = TeamsSession(tmp_path / "profile")
        with pytest.raises(TeamsLoginTimeout):
            await session.login_interactively(timeout_seconds=5)


@pytest.mark.asyncio
async def test_switching_headless_mode_closes_and_recreates_context(tmp_path):
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright()

    with patch("agents.teams.auth.async_playwright", return_value=mock_cm):
        session = TeamsSession(tmp_path / "profile")
        await session.is_logged_in()  # headless=True
        await session.login_interactively(timeout_seconds=5)  # headless=False

    # Two different headless modes requested -> two separate
    # launch_persistent_context calls (Chromium can't reuse one context
    # for both), and the first context must have been closed before the
    # second one launched (same profile_dir can't be opened twice).
    assert mock_chromium.launch_persistent_context.await_count == 2
    assert mock_context.close.await_count == 1


@pytest.mark.asyncio
async def test_reusing_same_headless_mode_does_not_recreate_context(tmp_path):
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright()

    with patch("agents.teams.auth.async_playwright", return_value=mock_cm):
        session = TeamsSession(tmp_path / "profile")
        await session.is_logged_in()
        await session.is_logged_in()

    assert mock_chromium.launch_persistent_context.await_count == 1


@pytest.mark.asyncio
async def test_close_stops_playwright_and_clears_state(tmp_path):
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright()

    with patch("agents.teams.auth.async_playwright", return_value=mock_cm):
        session = TeamsSession(tmp_path / "profile")
        await session.is_logged_in()
        await session.close()

    mock_context.close.assert_awaited()
    mock_cm.start.return_value.stop.assert_awaited_once()
    assert session._context is None
    assert session._playwright is None
