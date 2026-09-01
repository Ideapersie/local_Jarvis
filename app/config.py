"""Settings from .env plus the constants everything else imports.

Model names live here so no other module hardcodes a model string.
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import tzlocal
from dotenv import load_dotenv

log = logging.getLogger("jarvis.config")

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Paths
DB_PATH = ROOT / "jarvis.db"
DATABASE_URL = f"sqlite:///{DB_PATH}"
TEMPLATES_DIR = ROOT / "templates"
STATIC_DIR = ROOT / "static"
BRAIN_DIR = ROOT / "brain"
BRIEFS_DIR = ROOT / "briefs"

# Models. Quick tier for fixed-shape classification, agent tier for the loop,
# portfolio tier only where a wrong answer costs money.
MODEL_QUICK = "claude-haiku-4-5"
MODEL_AGENT = "claude-sonnet-5"
MODEL_PORTFOLIO = "claude-sonnet-5"

# Agent auth. "subscription" bills the monthly Agent SDK credit that comes with
# a Claude plan (Pro $20 / Max 5x $100 / Max 20x $200, no rollover); the SDK
# picks up the claude.ai login from ~/.claude/ when no API key reaches the
# subprocess. "api_key" is pay-as-you-go.
#
# The credit is the cheaper default, but it stops dead when exhausted - so the
# fallback matters: without it a spent credit means no 06:30 brief and no
# warning until you notice the panel is empty.
AGENT_AUTH = os.getenv("AGENT_AUTH", "subscription")  # subscription | api_key
AGENT_AUTH_FALLBACK = os.getenv("AGENT_AUTH_FALLBACK", "true").lower() == "true"

# Which plan's monthly credit to measure spend against on the dashboard.
# pro | max5 | max20 | none
PLAN = os.getenv("PLAN", "pro")

# Secrets
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
# No weather key: Open-Meteo is keyless and gives hourly data, which is what the
# two-lookup pattern needs. The spec's WEATHER_API_KEY is deliberately unused.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TRADING212_API_KEY = os.getenv("TRADING212_API_KEY")
TRADING212_API_SECRET = os.getenv("TRADING212_API_SECRET")
TRADING212_BASE_URL = os.getenv(
    "TRADING212_BASE_URL", "https://demo.trading212.com/api/v0"
)

DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION", "")


def resolve_timezone(env_tz: str | None, os_tz: str | None) -> str:
    """A filled TZ in .env wins; blank means follow the machine's own clock.

    Blank is the intended setting. Moving between London and Bangkok changes
    the laptop's zone and this follows it with no edit and no network call -
    nothing about where you are leaves the machine. Fill TZ only to pin a zone
    the laptop is wrong about.

    A zone ZoneInfo cannot load is dropped rather than trusted, because it would
    otherwise raise on the first timestamp the app writes.
    """
    for candidate in (env_tz, os_tz):
        if not candidate:
            continue
        try:
            ZoneInfo(candidate)
        except Exception:
            log.warning("timezone %r is not loadable, ignoring it", candidate)
            continue
        return candidate
    return "UTC"


def _machine_timezone() -> str | None:
    try:
        return tzlocal.get_localzone_name()
    except Exception:
        log.warning("could not read the machine timezone", exc_info=True)
        return None


TIMEZONE = resolve_timezone(os.getenv("TZ"), _machine_timezone())
TZ = ZoneInfo(TIMEZONE)


def now() -> datetime:
    """Local wall-clock time, deliberately NAIVE. Every timestamp goes through here.

    SQLite has no timezone type: SQLAlchemy writes a tz-aware datetime and reads
    it back naive, so mixing the two raises `can't subtract offset-naive and
    offset-aware datetimes` the first time anything compares a stored timestamp
    to the current one. Returning naive keeps both sides of every comparison in
    the same space.

    Every timestamp here means "when did this happen in my life" - one user, one
    timezone, and APScheduler takes its zone from the TZ string rather than from
    these values. The cost is that timestamps written before a permanent move to
    another timezone stay on the old wall clock.
    """
    return datetime.now(TZ).replace(tzinfo=None)


def today() -> date:
    return now().date()


DEFAULT_ROLLOVER_HOUR = 5


def resolve_rollover_hour(raw: str | None) -> int:
    """Parse DAY_ROLLOVER_HOUR, falling back rather than raising on nonsense.

    Capped below midday: a rollover at 13:00 would put an afternoon tick on
    yesterday, which is a misconfiguration and not a sleep schedule.
    """
    if not raw:
        return DEFAULT_ROLLOVER_HOUR
    try:
        hour = int(raw)
    except ValueError:
        log.warning("DAY_ROLLOVER_HOUR %r is not a number, ignoring it", raw)
        return DEFAULT_ROLLOVER_HOUR
    if not 0 <= hour <= 11:
        log.warning("DAY_ROLLOVER_HOUR %r is outside 0-11, ignoring it", raw)
        return DEFAULT_ROLLOVER_HOUR
    return hour


DAY_ROLLOVER_HOUR = resolve_rollover_hour(os.getenv("DAY_ROLLOVER_HOUR"))


DEFAULT_BACKFILL_DAYS = 2


def resolve_backfill_days(raw: str | None) -> int:
    """How many days back a missed tick may be corrected. 0 disables backfill.

    Capped at a week because the point is repairing a day you forgot to tick,
    not reconstructing a month from memory. A streak you cannot remember is not
    evidence of anything.
    """
    if not raw:
        return DEFAULT_BACKFILL_DAYS
    try:
        days = int(raw)
    except ValueError:
        log.warning("HABIT_BACKFILL_DAYS %r is not a number, ignoring it", raw)
        return DEFAULT_BACKFILL_DAYS
    if not 0 <= days <= 7:
        log.warning("HABIT_BACKFILL_DAYS %r is outside 0-7, ignoring it", raw)
        return DEFAULT_BACKFILL_DAYS
    return days


HABIT_BACKFILL_DAYS = resolve_backfill_days(os.getenv("HABIT_BACKFILL_DAYS"))


def habit_day(at: datetime | None = None) -> date:
    """The day a habit tick belongs to. Rolls at DAY_ROLLOVER_HOUR, not midnight.

    The user is normally still up at 01:00, so a tick then is the close of the
    day that just ended, not the opening of the next one. Scoring it by the
    calendar would break a streak on a day that was actually kept, and no
    amount of grace in the streak walk fixes that - the log row itself would
    carry the wrong date.

    Habits only. Calendar, weather and the brief stay on `today()`, because
    those answer "what is happening now", where the wall clock is right.
    """
    return ((at or now()) - timedelta(hours=DAY_ROLLOVER_HOUR)).date()
