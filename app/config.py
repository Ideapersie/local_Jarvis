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
MODEL_PORTFOLIO = "claude-opus-5"

# Secrets
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")
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
    """Local wall-clock time. Every date decision in the app goes through here."""
    return datetime.now(TZ)


def today() -> date:
    return now().date()
