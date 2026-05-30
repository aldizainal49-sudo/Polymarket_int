"""
weather_data_client.py
======================
Fetches temperature forecasts for a city/date.

Default provider: Open-Meteo (free, no API key).
Optional adapters (require WEATHER_API_KEY): WeatherAPI, Visual Crossing,
Meteostat. Each adapter returns the same normalized :class:`Forecast` object
so the rest of the bot is provider-agnostic.

Design notes
------------
* Everything is returned in Celsius internally. Callers convert to F as needed.
* A small built-in coordinate table covers the cities the bot is meant to
  trade (London, Paris, New York, Dallas, Singapore, ...). For anything else
  we fall back to Open-Meteo's geocoding endpoint.
* ``data_quality`` (0..1) reflects how complete/fresh the forecast is. If we
  cannot build a complete forecast, ``ok`` is False and the bot must SKIP.
* Network failures NEVER raise to the caller - they degrade to ok=False.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional

import requests

from config import Config, get_config
from utils import c_to_f, get_logger, safe_float, utcnow

log = get_logger("weather")

# Conservative network timeout; we never retry aggressively.
_HTTP_TIMEOUT = 15

# Built-in coordinates (lat, lon, IANA timezone) for the cities the bot
# is intended to trade. Avoids an extra geocoding round-trip and reduces
# the chance of a wrong-city match.
CITY_COORDS: Dict[str, Dict[str, object]] = {
    "london": {"lat": 51.5074, "lon": -0.1278, "tz": "Europe/London"},
    "paris": {"lat": 48.8566, "lon": 2.3522, "tz": "Europe/Paris"},
    "new york": {"lat": 40.7128, "lon": -74.0060, "tz": "America/New_York"},
    "nyc": {"lat": 40.7128, "lon": -74.0060, "tz": "America/New_York"},
    "dallas": {"lat": 32.7767, "lon": -96.7970, "tz": "America/Chicago"},
    "singapore": {"lat": 1.3521, "lon": 103.8198, "tz": "Asia/Singapore"},
    "los angeles": {"lat": 34.0522, "lon": -118.2437, "tz": "America/Los_Angeles"},
    "chicago": {"lat": 41.8781, "lon": -87.6298, "tz": "America/Chicago"},
    "miami": {"lat": 25.7617, "lon": -80.1918, "tz": "America/New_York"},
    "boston": {"lat": 42.3601, "lon": -71.0589, "tz": "America/New_York"},
    "philadelphia": {"lat": 39.9526, "lon": -75.1652, "tz": "America/New_York"},
    "houston": {"lat": 29.7604, "lon": -95.3698, "tz": "America/Chicago"},
    "denver": {"lat": 39.7392, "lon": -104.9903, "tz": "America/Denver"},
    "phoenix": {"lat": 33.4484, "lon": -112.0740, "tz": "America/Phoenix"},
    "seattle": {"lat": 47.6062, "lon": -122.3321, "tz": "America/Los_Angeles"},
    "washington": {"lat": 38.9072, "lon": -77.0369, "tz": "America/New_York"},
    "tokyo": {"lat": 35.6762, "lon": 139.6503, "tz": "Asia/Tokyo"},
    "berlin": {"lat": 52.5200, "lon": 13.4050, "tz": "Europe/Berlin"},
    "madrid": {"lat": 40.4168, "lon": -3.7038, "tz": "Europe/Madrid"},
    "moscow": {"lat": 55.7558, "lon": 37.6173, "tz": "Europe/Moscow"},
    "sydney": {"lat": -33.8688, "lon": 151.2093, "tz": "Australia/Sydney"},
    "toronto": {"lat": 43.6532, "lon": -79.3832, "tz": "America/Toronto"},
}


@dataclass
class Forecast:
    """Normalized forecast for one city/date (all temps in Celsius)."""

    city: str
    date: str  # YYYY-MM-DD (local to the city)
    provider: str
    ok: bool = False
    daily_high_c: Optional[float] = None
    daily_low_c: Optional[float] = None
    hourly: List[Dict[str, object]] = field(default_factory=list)  # [{time, temp_c}]
    forecast_update_time: Optional[str] = None
    data_quality: float = 0.0
    note: str = ""

    # Convenience accessors -------------------------------------------------
    @property
    def daily_high_f(self) -> Optional[float]:
        return c_to_f(self.daily_high_c) if self.daily_high_c is not None else None

    @property
    def daily_low_f(self) -> Optional[float]:
        return c_to_f(self.daily_low_c) if self.daily_low_c is not None else None

    def hourly_json(self) -> str:
        try:
            return json.dumps(self.hourly)
        except (TypeError, ValueError):
            return "[]"


class WeatherDataClient:
    """Provider-agnostic weather forecast client."""

    def __init__(self, config: Optional[Config] = None) -> None:
        self.config = config or get_config()
        self.provider = (self.config.weather_provider or "open_meteo").lower()
        self.api_key = self.config.weather_api_key
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "polymarket-weather-bot/1.0"})

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #
    def get_forecast(self, city: str, date: str) -> Forecast:
        """Return a normalized :class:`Forecast` for ``city`` on ``date``.

        ``date`` is the local calendar date (YYYY-MM-DD) the market resolves on.
        Never raises: on any problem returns a Forecast with ok=False.
        """
        city_norm = (city or "").strip().lower()
        if not city_norm or not date:
            return Forecast(city=city, date=date, provider=self.provider, note="missing city/date")

        try:
            if self.provider == "weatherapi":
                fc = self._fetch_weatherapi(city_norm, date)
            elif self.provider == "visual_crossing":
                fc = self._fetch_visual_crossing(city_norm, date)
            elif self.provider == "meteostat":
                fc = self._fetch_meteostat(city_norm, date)
            else:
                fc = self._fetch_open_meteo(city_norm, date)
        except requests.RequestException as exc:
            log.warning("Weather provider %s network error: %s", self.provider, exc)
            return Forecast(city=city, date=date, provider=self.provider, note=f"net error: {exc}")
        except Exception as exc:  # defensive: never crash the loop
            log.warning("Weather provider %s unexpected error: %s", self.provider, exc)
            return Forecast(city=city, date=date, provider=self.provider, note=f"error: {exc}")

        fc.data_quality = self._assess_quality(fc)
        fc.ok = fc.daily_high_c is not None and fc.daily_low_c is not None and fc.data_quality > 0
        return fc

    # ------------------------------------------------------------------ #
    # Geocoding                                                          #
    # ------------------------------------------------------------------ #
    def _resolve_coords(self, city_norm: str) -> Optional[Dict[str, object]]:
        if city_norm in CITY_COORDS:
            return CITY_COORDS[city_norm]
        # Fall back to Open-Meteo geocoding (works without an API key).
        try:
            resp = self.session.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params={"name": city_norm, "count": 1, "language": "en", "format": "json"},
                timeout=_HTTP_TIMEOUT,
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results") or []
            if not results:
                return None
            top = results[0]
            return {
                "lat": safe_float(top.get("latitude")),
                "lon": safe_float(top.get("longitude")),
                "tz": top.get("timezone", "UTC"),
            }
        except requests.RequestException as exc:
            log.warning("Geocoding failed for %s: %s", city_norm, exc)
            return None

    # ------------------------------------------------------------------ #
    # Open-Meteo (default)                                               #
    # ------------------------------------------------------------------ #
    def _fetch_open_meteo(self, city_norm: str, date: str) -> Forecast:
        coords = self._resolve_coords(city_norm)
        if not coords or coords.get("lat") is None:
            return Forecast(city=city_norm, date=date, provider="open_meteo", note="no coords")

        params = {
            "latitude": coords["lat"],
            "longitude": coords["lon"],
            "daily": "temperature_2m_max,temperature_2m_min",
            "hourly": "temperature_2m",
            "timezone": coords.get("tz", "auto"),
            "start_date": date,
            "end_date": date,
            "temperature_unit": "celsius",
        }
        resp = self.session.get(
            "https://api.open-meteo.com/v1/forecast", params=params, timeout=_HTTP_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()

        fc = Forecast(city=city_norm, date=date, provider="open_meteo")

        daily = data.get("daily") or {}
        highs = daily.get("temperature_2m_max") or []
        lows = daily.get("temperature_2m_min") or []
        if highs:
            fc.daily_high_c = safe_float(highs[0])
        if lows:
            fc.daily_low_c = safe_float(lows[0])

        hourly = data.get("hourly") or {}
        times = hourly.get("time") or []
        temps = hourly.get("temperature_2m") or []
        fc.hourly = [
            {"time": t, "temp_c": safe_float(temp)}
            for t, temp in zip(times, temps)
            if safe_float(temp) is not None
        ]

        # Open-Meteo exposes generationtime; use current fetch time as freshness.
        fc.forecast_update_time = utcnow().isoformat()
        return fc

    # ------------------------------------------------------------------ #
    # WeatherAPI.com adapter                                             #
    # ------------------------------------------------------------------ #
    def _fetch_weatherapi(self, city_norm: str, date: str) -> Forecast:
        fc = Forecast(city=city_norm, date=date, provider="weatherapi")
        if not self.api_key:
            fc.note = "weatherapi requires WEATHER_API_KEY"
            return fc
        resp = self.session.get(
            "https://api.weatherapi.com/v1/forecast.json",
            params={"key": self.api_key, "q": city_norm, "dt": date, "aqi": "no", "alerts": "no"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        forecast_days = (data.get("forecast") or {}).get("forecastday") or []
        if not forecast_days:
            fc.note = "no forecastday returned"
            return fc
        day0 = forecast_days[0]
        day_block = day0.get("day") or {}
        fc.daily_high_c = safe_float(day_block.get("maxtemp_c"))
        fc.daily_low_c = safe_float(day_block.get("mintemp_c"))
        hours = day0.get("hour") or []
        fc.hourly = [
            {"time": h.get("time"), "temp_c": safe_float(h.get("temp_c"))}
            for h in hours
            if safe_float(h.get("temp_c")) is not None
        ]
        fc.forecast_update_time = utcnow().isoformat()
        return fc

    # ------------------------------------------------------------------ #
    # Visual Crossing adapter                                            #
    # ------------------------------------------------------------------ #
    def _fetch_visual_crossing(self, city_norm: str, date: str) -> Forecast:
        fc = Forecast(city=city_norm, date=date, provider="visual_crossing")
        if not self.api_key:
            fc.note = "visual_crossing requires WEATHER_API_KEY"
            return fc
        url = (
            "https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/"
            f"timeline/{requests.utils.quote(city_norm)}/{date}/{date}"
        )
        resp = self.session.get(
            url,
            params={"key": self.api_key, "unitGroup": "metric", "include": "days,hours"},
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        days = data.get("days") or []
        if not days:
            fc.note = "no days returned"
            return fc
        day0 = days[0]
        fc.daily_high_c = safe_float(day0.get("tempmax"))
        fc.daily_low_c = safe_float(day0.get("tempmin"))
        hours = day0.get("hours") or []
        fc.hourly = [
            {"time": h.get("datetime"), "temp_c": safe_float(h.get("temp"))}
            for h in hours
            if safe_float(h.get("temp")) is not None
        ]
        fc.forecast_update_time = utcnow().isoformat()
        return fc

    # ------------------------------------------------------------------ #
    # Meteostat adapter (point/daily endpoint via RapidAPI-style key)    #
    # ------------------------------------------------------------------ #
    def _fetch_meteostat(self, city_norm: str, date: str) -> Forecast:
        fc = Forecast(city=city_norm, date=date, provider="meteostat")
        if not self.api_key:
            fc.note = "meteostat requires WEATHER_API_KEY"
            return fc
        coords = self._resolve_coords(city_norm)
        if not coords or coords.get("lat") is None:
            fc.note = "no coords"
            return fc
        resp = self.session.get(
            "https://meteostat.p.rapidapi.com/point/daily",
            params={
                "lat": coords["lat"],
                "lon": coords["lon"],
                "start": date,
                "end": date,
            },
            headers={
                "x-rapidapi-key": self.api_key,
                "x-rapidapi-host": "meteostat.p.rapidapi.com",
            },
            timeout=_HTTP_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("data") or []
        if not rows:
            fc.note = "no data rows"
            return fc
        row0 = rows[0]
        fc.daily_high_c = safe_float(row0.get("tmax"))
        fc.daily_low_c = safe_float(row0.get("tmin"))
        # Meteostat daily has no hourly; leave hourly empty (lowers quality).
        fc.forecast_update_time = utcnow().isoformat()
        fc.note = "meteostat daily (historical/observed; no hourly)"
        return fc

    # ------------------------------------------------------------------ #
    # Quality assessment                                                 #
    # ------------------------------------------------------------------ #
    def _assess_quality(self, fc: Forecast) -> float:
        """Score forecast completeness/freshness in [0, 1].

        Components:
          * daily high present       (0.35)
          * daily low present        (0.25)
          * hourly series present    (0.25, scaled by coverage up to 24 pts)
          * forecast freshness known (0.15)
        A forecast missing the high (the most-traded field) is heavily
        penalized so the bot skips.
        """
        score = 0.0
        if fc.daily_high_c is not None:
            score += 0.35
        if fc.daily_low_c is not None:
            score += 0.25
        if fc.hourly:
            coverage = min(len(fc.hourly), 24) / 24.0
            score += 0.25 * coverage
        if fc.forecast_update_time:
            score += 0.15
        return round(score, 4)


def get_forecast_for_market(
    city: str, date: str, config: Optional[Config] = None
) -> Forecast:
    """Convenience one-shot helper used by the scanner pipeline."""
    return WeatherDataClient(config).get_forecast(city, date)
