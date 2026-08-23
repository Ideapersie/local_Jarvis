"""Settings from .env plus the constants everything else imports.

Model names live here so no other module hardcodes a model string.
"""

from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

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

DEFAULT_LOCATION = os.getenv("DEFAULT_LOCATION", "bangkok")
TIMEZONE = os.getenv("TZ", "Asia/Bangkok")
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
