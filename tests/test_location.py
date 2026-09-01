"""Base city and timezone resolution.

No network and no clock reading: both functions take their inputs explicitly or
from module attributes a test can patch, so a machine in any timezone runs
these the same way.
"""

from __future__ import annotations

from app import config
from app.integrations import weather

# --- timezone ---------------------------------------------------------------


def test_env_timezone_overrides_the_machine_clock():
    # Filling TZ is a deliberate statement, so it beats whatever the OS says.
    assert config.resolve_timezone("Asia/Bangkok", "Europe/London") == "Asia/Bangkok"


def test_blank_env_timezone_follows_the_machine_clock():
    # Unset and empty-string must behave the same: .env carries `TZ=` with no
    # value, which dotenv loads as "" rather than leaving the name unset.
    assert config.resolve_timezone("", "Europe/London") == "Europe/London"
    assert config.resolve_timezone(None, "Europe/London") == "Europe/London"


def test_unreadable_machine_clock_falls_back_to_utc():
    assert config.resolve_timezone(None, None) == "UTC"


def test_unloadable_env_timezone_falls_through_to_the_machine_clock():
    # A typo in .env means the intent was still "use my local time", so drop to
    # the machine clock rather than stranding every timestamp on UTC.
    got = config.resolve_timezone("Mars/Olympus_Mons", "Europe/London")
    assert got == "Europe/London"


def test_utc_only_when_nothing_loadable_is_left():
    # A zone ZoneInfo cannot load would crash every timestamp in the app.
    assert config.resolve_timezone("Mars/Olympus_Mons", "Mars/Pavonis") == "UTC"


def test_unknown_env_location_warns(monkeypatch, caplog):
    monkeypatch.setattr(config, "DEFAULT_LOCATION", "paris")
    monkeypatch.setattr(config, "TIMEZONE", "Europe/London")

    with caplog.at_level("WARNING", logger="jarvis.weather"):
        weather.resolve_location(None)

    # Quietly ignoring a filled-in setting is how you end up trusting a forecast
    # you never actually configured.
    assert "paris" in caplog.text


# --- location ---------------------------------------------------------------


def test_event_text_beats_every_default(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_LOCATION", "")
    monkeypatch.setattr(config, "TIMEZONE", "Europe/London")
    assert weather.resolve_location("Standup, Bangkok office") == "bangkok"


def test_env_location_beats_the_timezone_map(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_LOCATION", "bangkok")
    monkeypatch.setattr(config, "TIMEZONE", "Europe/London")
    assert weather.resolve_location(None) == "bangkok"


def test_blank_env_location_follows_the_timezone(monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_LOCATION", "")
    monkeypatch.setattr(config, "TIMEZONE", "Europe/London")
    assert weather.resolve_location(None) == "london"


def test_unknown_env_location_is_ignored(monkeypatch):
    # "paris" has no coordinates, so honouring it would mean a lookup failure.
    monkeypatch.setattr(config, "DEFAULT_LOCATION", "paris")
    monkeypatch.setattr(config, "TIMEZONE", "Europe/London")
    assert weather.resolve_location(None) == "london"


def test_unmapped_timezone_warns_and_uses_the_final_fallback(monkeypatch, caplog):
    monkeypatch.setattr(config, "DEFAULT_LOCATION", "")
    monkeypatch.setattr(config, "TIMEZONE", "America/New_York")

    with caplog.at_level("WARNING", logger="jarvis.weather"):
        assert weather.resolve_location(None) == "bangkok"

    # Silence here is the failure mode worth guarding: the brief would report a
    # confident forecast for a city 5000km away.
    assert "America/New_York" in caplog.text


def test_every_mapped_zone_has_coordinates():
    # A zone mapped to a city with no coordinates raises KeyError inside
    # fetch(), which is the one path there deliberately is no fallback for.
    for zone, city in weather.TZ_CITY.items():
        assert city in weather.LOCATIONS, zone
