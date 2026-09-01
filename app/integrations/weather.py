"""Weather via Open-Meteo. No API key required.

The two-lookup pattern from spec section 8: for an event at a given hour, fetch
the hour before it and two hours after, and pass both as plain numbers. The
model is never handed a raw forecast blob and asked to reason about a time
window - it gets that wrong occasionally and you do not notice.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date, datetime

import httpx

from app import config

log = logging.getLogger("jarvis.weather")

API = "https://api.open-meteo.com/v1/forecast"

# Two cities only, so a lookup table beats a geocoding call. Fixed city-centre
# coordinates are also the whole privacy story here: Open-Meteo is asked about a
# city, never about where you actually are.
LOCATIONS: dict[str, tuple[float, float]] = {
    "bangkok": (13.7563, 100.5018),
    "london": (51.5072, -0.1276),
}

# Which city the machine's own clock implies. Reading the OS timezone costs
# nothing and sends nothing, so it beats asking the user to remember an edit.
TZ_CITY: dict[str, str] = {
    "Europe/London": "london",
    "Asia/Bangkok": "bangkok",
}

# Used only when the timezone is one of the many this table does not cover.
FALLBACK_LOCATION = "bangkok"

# Morning, midday, evening. One definition, used by both the header component
# and the brief prompt, so the two can never quote different hours.
ANCHOR_HOURS: tuple[int, int, int] = (8, 12, 18)

# A day's forecast does not move minute to minute, and the dashboard re-renders
# far more often than the forecast changes.
CACHE_TTL_SECONDS = 1800

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


def hour_label(hour: int) -> str:
    """24h hour as a 12h clock label. 8 -> 8am, 12 -> 12pm, 18 -> 6pm."""
    return f"{hour % 12 or 12}{'am' if hour < 12 else 'pm'}"


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

    def anchors(self) -> list[HourPoint | None]:
        """One slot per anchor hour, holes preserved.

        A missing hour stays None rather than being dropped: collapsing the list
        would shift the columns and relabel the evening temperature as midday.
        """
        return [self.hours.get(h) for h in ANCHOR_HOURS]

    def summary(self) -> str:
        """Morning, midday, evening. Enough for a brief with no event attached."""
        return "; ".join(str(p) for p in self.anchors() if p) or "no forecast"


def resolve_location(text: str | None) -> str:
    """Map free text to a known city, else fall back to where the machine is.

    Order: the event's own text, then a filled DEFAULT_LOCATION, then the city
    implied by the timezone, then FALLBACK_LOCATION. Two cities only, so a
    keyword match beats anything cleverer.

    Every fallback that is not a real answer logs, because a wrong guess here
    does not fail - it reports another city's weather in a confident sentence.
    """
    if text:
        low = text.lower()
        for name in LOCATIONS:
            if name in low:
                return name

    default = (config.DEFAULT_LOCATION or "").lower()
    if default in LOCATIONS:
        return default
    if default:
        log.warning("DEFAULT_LOCATION %r has no coordinates, ignoring it", default)

    city = TZ_CITY.get(config.TIMEZONE)
    if city:
        return city

    log.warning(
        "no city mapped to timezone %s, falling back to %s",
        config.TIMEZONE,
        FALLBACK_LOCATION,
    )
    return FALLBACK_LOCATION


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


# --- cache ------------------------------------------------------------------
#
# The dashboard header asks for the forecast on every page load. Without this
# that is one API call per render against a host that, on a bad connection,
# drops roughly half its TLS handshakes at a 15s timeout.

_cache: dict[tuple[str, date], tuple[float, DayWeather]] = {}


def clear_cache() -> None:
    _cache.clear()


def fetch_cached(
    location: str | None = None, day: date | None = None
) -> DayWeather | None:
    """fetch() with a short TTL, keyed on the resolved city and the day.

    Failures are deliberately not cached. fetch() returns None for a dropped
    connection as much as for a real outage, and caching that would turn one
    unlucky handshake into a blank panel for the rest of the TTL.
    """
    city = resolve_location(location)
    day = day or config.today()
    key = (city, day)

    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL_SECONDS:
        return hit[1]

    result = fetch(location=city, day=day)
    if result is not None:
        _cache[key] = (time.monotonic(), result)
    return result
