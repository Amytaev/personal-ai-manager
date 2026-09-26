from __future__ import annotations

import json

import httpx
import pytest

from agents.base import AgentStatus
from agents.weather import WeatherAgent


def _client_for(handler) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport)


def _geocoding_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "results": [
                {"name": "Almaty", "latitude": 43.25, "longitude": 76.95},
            ]
        },
    )


def _forecast_response(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "current": {
                "temperature_2m": 14.2,
                "apparent_temperature": 12.5,
                "weather_code": 61,
                "wind_speed_10m": 5.3,
            },
            "daily": {
                "time": ["2026-09-26", "2026-09-27", "2026-09-28"],
                "temperature_2m_max": [16.0, 15.0, 13.0],
                "temperature_2m_min": [8.0, 7.0, 6.0],
                "precipitation_probability_max": [65, 40, 10],
                "weather_code": [61, 3, 0],
            },
        },
    )


def _combined_handler(request: httpx.Request) -> httpx.Response:
    if "geocoding-api" in str(request.url):
        return _geocoding_response(request)
    return _forecast_response(request)


@pytest.mark.asyncio
async def test_successful_run_returns_working_with_normalized_data():
    agent = WeatherAgent("Almaty", http_client=_client_for(_combined_handler))
    result = await agent.run()
    await agent.aclose()

    assert result.status == AgentStatus.WORKING
    assert result.agent == "weather"
    assert result.data["location"] == "Almaty"
    assert result.data["current"]["temperature"] == 14.2
    assert result.data["current"]["feels_like"] == 12.5
    assert result.data["current"]["condition"] == "Дождь слабый"
    assert result.data["current"]["wind_speed"] == 5.3
    assert len(result.data["forecast"]) == 3
    assert result.data["forecast"][0]["temp_max"] == 16.0
    assert result.data["forecast"][0]["precipitation_probability"] == 65


@pytest.mark.asyncio
async def test_geocoding_caches_coordinates_across_multiple_runs():
    calls = {"geocode": 0, "forecast": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in str(request.url):
            calls["geocode"] += 1
            return _geocoding_response(request)
        calls["forecast"] += 1
        return _forecast_response(request)

    agent = WeatherAgent("Almaty", http_client=_client_for(handler))
    await agent.run()
    await agent.run()
    await agent.aclose()

    assert calls["geocode"] == 1  # geocoded once, cached after that
    assert calls["forecast"] == 2  # forecast re-fetched every run


@pytest.mark.asyncio
async def test_unknown_location_returns_failing_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"results": []})

    agent = WeatherAgent("Nowhereville", http_client=_client_for(handler))
    result = await agent.run()
    await agent.aclose()

    assert result.status == AgentStatus.FAILING
    assert "Nowhereville" in result.error


@pytest.mark.asyncio
async def test_http_error_returns_failing_after_retries(monkeypatch):
    # Speed up the test: don't actually wait through the backoff delays.
    import asyncio

    async def instant_sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)

    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in str(request.url):
            return _geocoding_response(request)
        attempts["count"] += 1
        return httpx.Response(503)

    agent = WeatherAgent("Almaty", http_client=_client_for(handler))
    result = await agent.run()
    await agent.aclose()

    assert result.status == AgentStatus.FAILING
    assert attempts["count"] == 3  # retried up to max_attempts, then gave up


@pytest.mark.asyncio
async def test_missing_precipitation_field_does_not_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in str(request.url):
            return _geocoding_response(request)
        return httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 10,
                    "apparent_temperature": 9,
                    "weather_code": 0,
                    "wind_speed_10m": 2,
                },
                "daily": {
                    "time": ["2026-09-26"],
                    "temperature_2m_max": [12],
                    "temperature_2m_min": [5],
                    "weather_code": [0],
                    # precipitation_probability_max intentionally absent
                },
            },
        )

    agent = WeatherAgent("Almaty", http_client=_client_for(handler))
    result = await agent.run()
    await agent.aclose()

    assert result.status == AgentStatus.WORKING
    assert result.data["forecast"][0]["precipitation_probability"] is None


@pytest.mark.asyncio
async def test_mismatched_daily_array_lengths_truncates_instead_of_crashing():
    # A real API misbehaving: `time` promises 3 days but temperature_2m_max
    # only has 2 entries. Must not raise IndexError - either truncate
    # cleanly or report FAILING, never crash run_isolated()'s caller.
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in str(request.url):
            return _geocoding_response(request)
        return httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 10,
                    "apparent_temperature": 9,
                    "weather_code": 0,
                    "wind_speed_10m": 2,
                },
                "daily": {
                    "time": ["2026-09-26", "2026-09-27", "2026-09-28"],
                    "temperature_2m_max": [12, 13],  # short by one
                    "temperature_2m_min": [5, 6, 7],
                    "precipitation_probability_max": [10, 20, 30],
                    "weather_code": [0, 1, 2],
                },
            },
        )

    agent = WeatherAgent("Almaty", http_client=_client_for(handler))
    result = await agent.run()
    await agent.aclose()

    assert result.status == AgentStatus.WORKING
    assert len(result.data["forecast"]) == 2  # truncated to the shortest array


@pytest.mark.asyncio
async def test_missing_required_daily_field_reports_failing_not_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in str(request.url):
            return _geocoding_response(request)
        return httpx.Response(
            200,
            json={
                "current": {
                    "temperature_2m": 10,
                    "apparent_temperature": 9,
                    "weather_code": 0,
                    "wind_speed_10m": 2,
                },
                "daily": {
                    "time": ["2026-09-26"],
                    "temperature_2m_max": [12],
                    # temperature_2m_min and weather_code entirely absent
                },
            },
        )

    agent = WeatherAgent("Almaty", http_client=_client_for(handler))
    result = await agent.run()
    await agent.aclose()

    assert result.status == AgentStatus.FAILING
    assert "Unexpected Open-Meteo response shape" in result.error
