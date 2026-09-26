from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.teams.auth import TeamsLoginTimeout, TeamsSession


def _make_mock_playwright(page_url: str = "https://teams.microsoft.com/v2/"):
    """Builds a fake async_playwright().start() result whose chromium
    launches a context/page that we fully control from the test."""
    mock_page = AsyncMock()
    mock_page.url = page_url
    mock_page.goto = AsyncMock()
    mock_page.wait_for_url = AsyncMock()
    mock_page.close = AsyncMock()

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
async def test_is_logged_in_true_when_url_matches(tmp_path):
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright()

    with patch("agents.teams.auth.async_playwright", return_value=mock_cm):
        session = TeamsSession(tmp_path / "profile")
        result = await session.is_logged_in()

    assert result is True
    mock_chromium.launch_persistent_context.assert_awaited_once()
    call_kwargs = mock_chromium.launch_persistent_context.call_args.kwargs
    assert call_kwargs["headless"] is True


@pytest.mark.asyncio
async def test_is_logged_in_false_when_wait_for_url_times_out(tmp_path):
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright()
    mock_page.wait_for_url = AsyncMock(side_effect=TimeoutError("stuck on login page"))

    with patch("agents.teams.auth.async_playwright", return_value=mock_cm):
        session = TeamsSession(tmp_path / "profile")
        result = await session.is_logged_in()

    assert result is False


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
