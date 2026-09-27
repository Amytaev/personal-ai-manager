from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.sso.auth import IS_AUTHENTICATED_URL, SsoAutofillLoginFailed, SsoLoginTimeout, SsoSession


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


def _wire_autofill_page(mock_page) -> None:
    """login_via_autofill() calls page.wait_for_function() (awaited) and
    page.locator(...).first.click() (locator() is a SYNC call in real
    Playwright, only .click() is async) - _make_mock_playwright()'s
    plain AsyncMock page doesn't shape those two correctly by default,
    so tests using them wire this in first."""
    mock_page.wait_for_function = AsyncMock()
    mock_locator = MagicMock()
    mock_locator.first = MagicMock()
    mock_locator.first.click = AsyncMock()
    mock_page.locator = MagicMock(return_value=mock_locator)


@pytest.mark.asyncio
async def test_login_via_autofill_succeeds_when_password_fills_and_login_confirmed(tmp_path):
    mock_cm, _, mock_context, mock_page = _make_mock_playwright()
    _wire_autofill_page(mock_page)

    with patch("agents.sso.auth.async_playwright", return_value=mock_cm):
        session = SsoSession(tmp_path / "profile")
        # is_logged_in() is called twice inside login_via_autofill(): the
        # docstring's step 4 confirmation, called here directly (not
        # through the module's HTTP mock) since it's the session's own
        # method - False then True mirrors a real "not authenticated
        # yet, then confirmed after clicking" sequence.
        session.is_logged_in = AsyncMock(return_value=True)
        result = await session.login_via_autofill()

    assert result is True
    mock_page.wait_for_function.assert_awaited_once()
    mock_page.locator.assert_called_once_with("button:has-text('войти')")
    mock_locator = mock_page.locator.return_value
    mock_locator.first.click.assert_awaited_once()
    mock_context.new_page.assert_awaited()


@pytest.mark.asyncio
async def test_login_via_autofill_raises_when_password_never_fills(tmp_path):
    mock_cm, _, _, mock_page = _make_mock_playwright()
    _wire_autofill_page(mock_page)
    mock_page.wait_for_function = AsyncMock(side_effect=TimeoutError("no autofill"))

    with patch("agents.sso.auth.async_playwright", return_value=mock_cm):
        session = SsoSession(tmp_path / "profile")
        with pytest.raises(SsoAutofillLoginFailed, match="never autofilled"):
            await session.login_via_autofill(timeout_seconds=1)

    # Never even tried to click a button it had no evidence was ready.
    mock_page.locator.assert_not_called()


@pytest.mark.asyncio
async def test_login_via_autofill_raises_when_still_not_authenticated_after_click(tmp_path):
    mock_cm, _, _, mock_page = _make_mock_playwright()
    _wire_autofill_page(mock_page)

    with patch("agents.sso.auth.async_playwright", return_value=mock_cm):
        session = SsoSession(tmp_path / "profile")
        # Saved credentials are stale (real password changed) - button
        # gets clicked fine, but the real IsAuthenticated check after it
        # still says no.
        session.is_logged_in = AsyncMock(return_value=False)
        with pytest.raises(SsoAutofillLoginFailed, match="stale"):
            await session.login_via_autofill()

    mock_page.locator.return_value.first.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_login_via_autofill_defaults_to_headless(tmp_path):
    mock_cm, mock_chromium, _, mock_page = _make_mock_playwright()
    _wire_autofill_page(mock_page)

    with patch("agents.sso.auth.async_playwright", return_value=mock_cm):
        session = SsoSession(tmp_path / "profile")
        session.is_logged_in = AsyncMock(return_value=True)
        await session.login_via_autofill()

    # Defaults to headless=True on purpose - see login_via_autofill()'s
    # docstring on why (avoids the headed->headless close/reopen cookie-
    # flush race is_logged_in() would otherwise trigger mid-call).
    call_kwargs = mock_chromium.launch_persistent_context.call_args.kwargs
    assert call_kwargs["headless"] is True


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
