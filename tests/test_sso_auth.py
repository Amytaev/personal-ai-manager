from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.sso.auth import IS_AUTHENTICATED_URL, SsoLoginTimeout, SsoSession


def _fake_api_response(ok: bool, body):
    response = MagicMock()
    response.ok = ok
    response.json = AsyncMock(return_value=body)
    return response


def _make_mock_playwright(auth_ok: bool = True, auth_body=True, page_url: str = "https://sso.satbayev.university/"):
    """Builds a fake async_playwright().start() result whose chromium
    launches a context/page we fully control from the test - mirrors
    tests/test_valorant_auth.py's helper of the same shape, plus a fake
    context.request.get() for the Auth/IsAuthenticated check (see
    agents/sso/auth.py's docstring for why is_logged_in() calls that
    endpoint directly instead of comparing a page URL)."""
    mock_page = AsyncMock()
    mock_page.url = page_url
    mock_page.goto = AsyncMock()
    mock_page.close = AsyncMock()
    mock_page.wait_for_timeout = AsyncMock()

    mock_request = MagicMock()
    mock_request.get = AsyncMock(return_value=_fake_api_response(auth_ok, auth_body))

    mock_context = AsyncMock()
    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.close = AsyncMock()
    mock_context.request = mock_request

    mock_chromium = MagicMock()
    mock_chromium.launch_persistent_context = AsyncMock(return_value=mock_context)

    mock_playwright_instance = MagicMock()
    mock_playwright_instance.chromium = mock_chromium
    mock_playwright_instance.stop = AsyncMock()

    mock_playwright_cm = MagicMock()
    mock_playwright_cm.start = AsyncMock(return_value=mock_playwright_instance)

    return mock_playwright_cm, mock_chromium, mock_context, mock_page


@pytest.mark.asyncio
async def test_is_logged_in_true_when_endpoint_returns_true(tmp_path):
    mock_cm, mock_chromium, mock_context, _ = _make_mock_playwright(auth_ok=True, auth_body=True)

    with patch("agents.sso.auth.async_playwright", return_value=mock_cm):
        session = SsoSession(tmp_path / "profile")
        result = await session.is_logged_in()

    assert result is True
    mock_context.request.get.assert_awaited_once()
    call_args = mock_context.request.get.call_args
    assert call_args.args[0] == IS_AUTHENTICATED_URL
    call_kwargs = mock_chromium.launch_persistent_context.call_args.kwargs
    assert call_kwargs["headless"] is True
    # Real bug found live (2026-09-27): context.request.get() against
    # api.satbayev.university failed TLS verification (missing
    # intermediate cert, no AIA chasing in Playwright's Node-based
    # request stack) - see agents/sso/auth.py's docstring at this call.
    assert call_kwargs["ignore_https_errors"] is True


@pytest.mark.asyncio
async def test_is_logged_in_false_when_body_is_not_literal_true(tmp_path):
    mock_cm, _, _, _ = _make_mock_playwright(auth_ok=True, auth_body=False)

    with patch("agents.sso.auth.async_playwright", return_value=mock_cm):
        session = SsoSession(tmp_path / "profile")
        result = await session.is_logged_in()

    assert result is False


@pytest.mark.asyncio
async def test_is_logged_in_false_when_response_not_ok(tmp_path):
    mock_cm, _, _, _ = _make_mock_playwright(auth_ok=False, auth_body=None)

    with patch("agents.sso.auth.async_playwright", return_value=mock_cm):
        session = SsoSession(tmp_path / "profile")
        result = await session.is_logged_in()

    assert result is False


@pytest.mark.asyncio
async def test_is_logged_in_false_when_body_is_not_json(tmp_path):
    mock_cm, mock_chromium, mock_context, _ = _make_mock_playwright(auth_ok=True, auth_body=True)
    mock_context.request.get = AsyncMock(
        return_value=MagicMock(ok=True, json=AsyncMock(side_effect=ValueError("not json")))
    )

    with patch("agents.sso.auth.async_playwright", return_value=mock_cm):
        session = SsoSession(tmp_path / "profile")
        result = await session.is_logged_in()

    assert result is False


@pytest.mark.asyncio
async def test_login_interactively_returns_url_once_logged_in(tmp_path):
    mock_cm, mock_chromium, mock_context, mock_page = _make_mock_playwright(page_url="https://sso.satbayev.university/dashboard")

    with patch("agents.sso.auth.async_playwright", return_value=mock_cm):
        session = SsoSession(tmp_path / "profile")
        # First poll: not logged in yet: second poll: logged in - proves
        # login_interactively actually polls is_logged_in() rather than
        # trusting the first check.
        session.is_logged_in = AsyncMock(side_effect=[False, True])
        final_url = await session.login_interactively(timeout_seconds=5)

    assert final_url == "https://sso.satbayev.university/dashboard"
    call_kwargs = mock_chromium.launch_persistent_context.call_args.kwargs
    assert call_kwargs["headless"] is False


@pytest.mark.asyncio
async def test_login_interactively_raises_timeout_when_never_logged_in(tmp_path):
    mock_cm, _, _, mock_page = _make_mock_playwright()

    with patch("agents.sso.auth.async_playwright", return_value=mock_cm):
        session = SsoSession(tmp_path / "profile")
        session.is_logged_in = AsyncMock(return_value=False)
        with pytest.raises(SsoLoginTimeout):
            await session.login_interactively(timeout_seconds=0)


@pytest.mark.asyncio
async def test_switching_headless_mode_closes_and_recreates_context(tmp_path):
    mock_cm, mock_chromium, mock_context, _ = _make_mock_playwright()

    with patch("agents.sso.auth.async_playwright", return_value=mock_cm):
        session = SsoSession(tmp_path / "profile")
        await session.is_logged_in()  # headless=True
        session.is_logged_in = AsyncMock(return_value=True)
        await session.login_interactively(timeout_seconds=5)  # headless=False

    assert mock_chromium.launch_persistent_context.await_count == 2
    assert mock_context.close.await_count == 1


@pytest.mark.asyncio
async def test_reusing_same_headless_mode_does_not_recreate_context(tmp_path):
    mock_cm, mock_chromium, mock_context, _ = _make_mock_playwright()

    with patch("agents.sso.auth.async_playwright", return_value=mock_cm):
        session = SsoSession(tmp_path / "profile")
        await session.is_logged_in()
        await session.is_logged_in()

    assert mock_chromium.launch_persistent_context.await_count == 1


@pytest.mark.asyncio
async def test_close_stops_playwright_and_clears_state(tmp_path):
    mock_cm, mock_chromium, mock_context, _ = _make_mock_playwright()

    with patch("agents.sso.auth.async_playwright", return_value=mock_cm):
        session = SsoSession(tmp_path / "profile")
        await session.is_logged_in()
        await session.close()

    mock_context.close.assert_awaited()
    mock_cm.start.return_value.stop.assert_awaited_once()
    assert session._context is None
    assert session._playwright is None
