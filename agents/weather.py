"""Weather Agent (TZ v4 §10-11, Phase 4).

Uses Open-Meteo - chosen in the Phase 1 Architecture Review: no API
key, generous free tier for a personal project's polling frequency,
documented multi-day forecast. Two HTTP calls per cycle: geocoding
(location name -> lat/lon, resolved once and cached for the agent's
lifetime - the location doesn't change between runs) and the actual
forecast request.

WeatherAPI.com stays a possible future fallback for weather *alerts*
specifically (Open-Meteo doesn't provide them) - see the Phase 1
report. Not added here; add only if/when alerts are actually wanted.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from agents.base import AgentResult, AgentStatus, BaseAgent
from utils.retry import retry_with_backoff

logger = logging.getLogger(__name__)

GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# WMO weather interpretation codes, as used by Open-Meteo.
# https://open-meteo.com/en/docs -> "WMO Weather interpretation codes"
WEATHER_CODES = {
    0: "Ясно",
    1: "Преимущественно ясно",
    2: "Переменная облачность",
    3: "Пасмурно",
    45: "Туман",
    48: "Изморозь",
    51: "Морось слабая",
    53: "Морось умеренная",
    55: "Морось сильная",
    56: "Ледяная морось слабая",
    57: "Ледяная морось сильная",
    61: "Дождь слабый",
    63: "Дождь умеренный",
    65: "Дождь сильный",
    66: "Ледяной дождь слабый",
    67: "Ледяной дождь сильный",
    71: "Снег слабый",
    73: "Снег умеренный",
    75: "Снег сильный",
    77: "Снежные зёрна",
    80: "Ливень слабый",
    81: "Ливень умеренный",
    82: "Ливень сильный",
    85: "Снегопад слабый",
    86: "Снегопад сильный",
    95: "Гроза",
    96: "Гроза с градом (слабая)",
    99: "Гроза с градом (сильная)",
}


def describe_weather_code(code: int | None) -> str:
    if code is None:
        return "Неизвестно"
    return WEATHER_CODES.get(code, f"Код {code}")


@dataclass
class Coordinates:
    latitude: float
    longitude: float
    resolved_name: str


class WeatherAgent(BaseAgent):
    def __init__(self, location: str, http_client: httpx.AsyncClient | None = None):
        super().__init__(name="weather")
        self.location = location
        # Reused across every run() call - NOT recreated per request. A
        # fresh client per call was flagged as an anti-pattern in the
        # Phase 2 LLMProvider review; not repeating that mistake here.
        self._client = http_client or httpx.AsyncClient(timeout=10.0)
        self._owns_client = http_client is None
        self._coords: Coordinates | None = None

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @retry_with_backoff(max_attempts=3, base_delay=1.0, max_delay=5.0, exceptions=(httpx.HTTPError,))
    async def _geocode(self) -> Coordinates:
        response = await self._client.get(
            GEOCODING_URL,
            params={"name": self.location, "count": 1, "language": "ru", "format": "json"},
        )
        response.raise_for_status()
        results = response.json().get("results") or []
        if not results:
            raise ValueError(f"Location not found by Open-Meteo geocoding: {self.location!r}")
        top = results[0]
        return Coordinates(
            latitude=top["latitude"],
            longitude=top["longitude"],
            resolved_name=top.get("name", self.location),
        )

    @retry_with_backoff(max_attempts=3, base_delay=1.0, max_delay=5.0, exceptions=(httpx.HTTPError,))
    async def _fetch_forecast(self, coords: Coordinates) -> dict:
        response = await self._client.get(
            FORECAST_URL,
            params={
                "latitude": coords.latitude,
                "longitude": coords.longitude,
                "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
                "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
                "timezone": "auto",
                "forecast_days": 3,
            },
        )
        response.raise_for_status()
        return response.json()

    async def run(self) -> AgentResult:
        try:
            if self._coords is None:
                self._coords = await self._geocode()
            raw = await self._fetch_forecast(self._coords)
        except (httpx.HTTPError, ValueError) as exc:
            # Expected failure modes (API down, bad location, timeout) -
            # reported as FAILING, not raised. run_isolated() in
            # agents/base.py is still the backstop for anything
            # unexpected (TZ v4 §10/§21).
            return AgentResult(agent=self.name, status=AgentStatus.FAILING, error=str(exc))

        current = raw.get("current", {})
        daily = raw.get("daily", {})
        days = daily.get("time", [])
        precipitation = daily.get("precipitation_probability_max", [None] * len(days))

        try:
            # Don't assume Open-Meteo's daily arrays are all the same
            # length as `time` - if the API ever returns a short/missing
            # array, iterate only as far as every field we index actually
            # has data, rather than risking an IndexError/KeyError deep
            # inside a list comprehension (caught here explicitly so the
            # error message says *what* was inconsistent, rather than
            # relying on run_isolated()'s generic backstop to catch it).
            required_lengths = [
                len(days),
                len(daily["temperature_2m_max"]),
                len(daily["temperature_2m_min"]),
                len(daily["weather_code"]),
            ]
            forecast_len = min(required_lengths)
            forecast = [
                {
                    "date": days[i],
                    "temp_max": daily["temperature_2m_max"][i],
                    "temp_min": daily["temperature_2m_min"][i],
                    "precipitation_probability": precipitation[i] if i < len(precipitation) else None,
                    "condition": describe_weather_code(daily["weather_code"][i]),
                }
                for i in range(forecast_len)
            ]
        except KeyError as exc:
            return AgentResult(
                agent=self.name,
                status=AgentStatus.FAILING,
                error=f"Unexpected Open-Meteo response shape, missing field: {exc}",
            )

        data = {
            "location": self._coords.resolved_name,
            "current": {
                "temperature": current.get("temperature_2m"),
                "feels_like": current.get("apparent_temperature"),
                "condition": describe_weather_code(current.get("weather_code")),
                "wind_speed": current.get("wind_speed_10m"),
            },
            "forecast": forecast,
        }
        return AgentResult(agent=self.name, status=AgentStatus.WORKING, data=data)
