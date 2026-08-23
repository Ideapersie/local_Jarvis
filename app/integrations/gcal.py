"""Calendar reads: today, and the next seven days.

Timed and all-day events are kept apart. An all-day entry has no hour, so it
cannot be bracketed for weather and must not be described as happening "at" a
time - a birthday reminder is not a 00:00 appointment.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from app import config
from app.integrations import google_auth

log = logging.getLogger("jarvis.gcal")

MAX_EVENTS = 50

# Words suggesting an event happens outdoors, where the weather actually
# changes the plan. Deliberately a small list: a false positive costs one extra
# forecast lookup, which is free and cached.
OUTDOOR_HINTS = (
    "football",
    "run",
    "running",
    "park",
    "walk",
    "cycle",
    "cycling",
    "tennis",
    "golf",
    "hike",
    "hiking",
    "outdoor",
    "picnic",
    "garden",
    "beach",
    "market",
)


@dataclass
class Event:
    summary: str
    start: datetime | None  # None for all-day
    day: date
    all_day: bool
    location: str | None = None

    @property
    def hour(self) -> int | None:
        return self.start.hour if self.start else None

    @property
    def outdoors(self) -> bool:
        """Whether weather plausibly matters for this event."""
        if self.all_day:
            return False
        text = f"{self.summary} {self.location or ''}".lower()
        return any(word in text for word in OUTDOOR_HINTS)

    def __str__(self) -> str:
        when = "all day" if self.all_day else self.start.strftime("%H:%M")
        where = f" @ {self.location}" if self.location else ""
        return f"{when} {self.summary}{where}"


def available() -> bool:
    return google_auth.configured()


def _parse(node: dict) -> tuple[datetime | None, date | None, bool]:
    """Google returns dateTime for timed events and date for all-day ones."""
    if "dateTime" in node:
        try:
            stamp = datetime.fromisoformat(node["dateTime"])
        except ValueError:
            return None, None, False
        # Drop tzinfo to match the naive-local convention used everywhere else.
        return stamp.replace(tzinfo=None), stamp.date(), False
    if "date" in node:
        try:
            day = date.fromisoformat(node["date"])
        except ValueError:
            return None, None, False
        return None, day, True
    return None, None, False


def upcoming(days: int = 7, start: date | None = None) -> list[Event]:
    """Events from `start` through `days` ahead. Never raises."""
    start = start or config.today()
    window_end = start + timedelta(days=days)

    try:
        svc = google_auth.service("calendar", "v3")
        result = (
            svc.events()
            .list(
                calendarId="primary",
                timeMin=datetime.combine(start, time.min).isoformat() + "Z",
                timeMax=datetime.combine(window_end, time.max).isoformat() + "Z",
                singleEvents=True,
                orderBy="startTime",
                maxResults=MAX_EVENTS,
            )
            .execute()
        )
    except Exception:
        log.warning("calendar fetch failed", exc_info=True)
        return []

    events: list[Event] = []
    for item in result.get("items", []):
        stamp, day, all_day = _parse(item.get("start", {}))
        if day is None:
            continue
        events.append(
            Event(
                summary=item.get("summary", "(untitled)"),
                start=stamp,
                day=day,
                all_day=all_day,
                location=(item.get("location") or "").strip() or None,
            )
        )
    return events


def today(events: list[Event], day: date | None = None) -> list[Event]:
    day = day or config.today()
    return [e for e in events if e.day == day]
