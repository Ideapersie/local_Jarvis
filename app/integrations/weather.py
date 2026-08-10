"""Weather via Open-Meteo. No API key required.

The two-lookup pattern from spec section 8: for an event at a given hour, fetch
the hour before it and two hours after, and pass both as plain numbers. The
model is never handed a raw forecast blob and asked to reason about a time
window - it gets that wrong occasionally and you do not notice.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from app import config

log = logging.getLogger("jarvis.weather")

API = "https://api.open-meteo.com/v1/forecast"

# Two cities only, so a lookup table beats a geocoding call.
LOCATIONS: dict[str, tuple[float, float]] = {
    "bangkok": (13.7563, 100.5018),
    "london": (51.5072, -0.1276),
}

# WMO weather codes, condensed to what a brief actually needs.
_WMO = {
    0: "clear",
    1: "mostly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "freezing fog",
    51: "light drizzle",
    53: "drizzle",
    55: "heavy drizzle",
    61: "light rain",
    63: "rain",
    65: "heavy rain",
    71: "light snow",
    73: "snow",
    75: "heavy snow",
    80: "light showers",
    81: "showers",
    82: "violent showers",
    95: "thunderstorm",
    96: "thunderstorm with hail",
    99: "severe thunderstorm with hail",
}


def describe(code: int | None) -> str:
    return _WMO.get(code, "unknown") if code is not None else "unknown"


@dataclass(frozen=True)
class HourPoint:
    hour: int
    temp_c: float | None
    rain_pct: int | None
    condition: str

    def __str__(self) -> str:
        t = f"{self.temp_c:.0f}C" if self.temp_c is not None else "?"
        r = f"{self.rain_pct}% rain" if self.rain_pct is not None else "? rain"
        return f"{self.hour:02d}:00 {t}, {r}, {self.condition}"


@dataclass(frozen=True)
class DayWeather:
    location: str
    day: date
    hours: dict[int, HourPoint]

    def at(self, hour: int) -> HourPoint | None:
        return self.hours.get(max(0, min(23, hour)))

    def around(self, event_hour: int) -> tuple[HourPoint | None, HourPoint | None]:
        """The spec's two lookups: one hour before, two hours after."""
        return self.at(event_hour - 1), self.at(event_hour + 2)

    def summary(self) -> str:
        """Morning, midday, evening. Enough for a brief with no event attached."""
        picks = [self.at(h) for h in (8, 13, 19)]
        return "; ".join(str(p) for p in picks if p) or "no forecast"


def resolve_location(text: str | None) -> str:
    """Map free text to a known city, defaulting to the configured one.

    Two cities only, so a keyword match beats anything cleverer - and a wrong
    guess here silently reports the wrong city's weather.
    """
    if text:
        low = text.lower()
        for name in LOCATIONS:
            if name in low:
                return name
    default = (config.DEFAULT_LOCATION or "bangkok").lower()
    return default if default in LOCATIONS else "bangkok"


def fetch(location: str | None = None, day: date | None = None) -> DayWeather | None:
    """Hourly forecast for one day. Returns None on failure rather than raising.

    A brief without weather is still a brief; a brief that crashes at 06:30 is
    not.
    """
    name = resolve_location(location)
    lat, lon = LOCATIONS[name]
    day = day or config.today()

    try:
        r = httpx.get(
            API,
            params={
                "latitude": lat,
                "longitude": lon,
                "hourly": "temperature_2m,precipitation_probability,weathercode",
                "start_date": day.isoformat(),
                "end_date": day.isoformat(),
                "timezone": config.TIMEZONE,
            },
            timeout=15.0,
        )
        r.raise_for_status()
        data = r.json()["hourly"]
    except Exception:
        log.warning("weather fetch failed for %s", name, exc_info=True)
        return None

    hours: dict[int, HourPoint] = {}
    for i, stamp in enumerate(data.get("time", [])):
        try:
            hour = datetime.fromisoformat(stamp).hour
        except ValueError:
            continue
        hours[hour] = HourPoint(
            hour=hour,
            temp_c=_idx(data.get("temperature_2m"), i),
            rain_pct=_idx(data.get("precipitation_probability"), i),
            condition=describe(_idx(data.get("weathercode"), i)),
        )

    return DayWeather(location=name, day=day, hours=hours)


def _idx(seq: list | None, i: int):
    if not seq or i >= len(seq):
        return None
    return seq[i]
