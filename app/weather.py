"""Outdoor heat from Open-Meteo (free, no API key).

Live mode reads today's forecast. Demo mode replays a *real* past day from the
Open-Meteo historical archive, so the heat numbers on screen are never invented.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
DAILY = "temperature_2m_max,apparent_temperature_max"
CACHE_S = 30 * 60


@dataclass
class Weather:
    date: str
    temp_max_c: float | None
    apparent_max_c: float | None
    source: str
    mode: str  # live | replay | fallback
    fetched_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "date": self.date,
            "temp_max_c": self.temp_max_c,
            "apparent_max_c": self.apparent_max_c,
            "source": self.source,
            "mode": self.mode,
        }


class WeatherService:
    def __init__(self, timeout: float = 6.0):
        self.timeout = timeout
        self.current: Weather | None = None
        self.replay_date: str | None = None
        self.replay_fallback: tuple[float, float] | None = None
        self.last_error: str | None = None

    async def _daily(self, url: str, params: dict) -> tuple[str, float, float]:
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            daily = resp.json()["daily"]
        return daily["time"][0], daily["temperature_2m_max"][0], daily["apparent_temperature_max"][0]

    async def refresh_live(self, lat: float, lon: float, tz: str, place: str) -> Weather:
        if self.current and self.current.mode == "live" and time.time() - self.current.fetched_at < CACHE_S:
            return self.current
        params = {"latitude": lat, "longitude": lon, "daily": DAILY, "timezone": tz, "forecast_days": 1}
        try:
            day, tmax, amax = await self._daily(FORECAST_URL, params)
            self.current = Weather(day, tmax, amax, f"Open-Meteo forecast · {place}", "live")
            self.last_error = None
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            self.last_error = f"Open-Meteo unavailable: {exc}"
            if self.current is None:
                self.current = Weather("", None, None, "Weather unavailable", "fallback")
        return self.current

    async def load_replay(
        self, day: str, lat: float, lon: float, tz: str, place: str, fallback: tuple[float, float] | None
    ) -> Weather:
        """Load the observed weather of a past day (Open-Meteo archive / ERA5 reanalysis)."""
        self.replay_date = day
        self.replay_fallback = fallback
        params = {
            "latitude": lat,
            "longitude": lon,
            "start_date": day,
            "end_date": day,
            "daily": DAILY,
            "timezone": tz,
        }
        try:
            got_day, tmax, amax = await self._daily(ARCHIVE_URL, params)
            self.current = Weather(
                got_day, tmax, amax, f"Open-Meteo historical archive · {place} · {got_day}", "replay"
            )
            self.last_error = None
        except (httpx.HTTPError, KeyError, IndexError, ValueError) as exc:
            self.last_error = f"Open-Meteo archive unavailable: {exc}"
            tmax, amax = fallback if fallback else (None, None)
            self.current = Weather(
                day, tmax, amax, f"Offline fallback values for {place} · {day}", "fallback"
            )
        return self.current
