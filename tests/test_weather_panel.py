"""Header weather component: anchor hours, caching, and the panel route.

No network anywhere. fetch() is monkeypatched throughout, because the real API
drops connections often enough that a test touching it would be flaky by design.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.integrations import weather

DAY = date(2026, 8, 27)


def point(hour, temp, rain, condition="overcast"):
    return weather.HourPoint(
        hour=hour, temp_c=temp, rain_pct=rain, condition=condition
    )


DEFAULT_HOURS = {
    8: point(8, 20.0, 96),
    12: point(12, 22.0, 100),
    18: point(18, 24.0, 29),
}


def day_weather(hours=None):
    return weather.DayWeather(
        location="london", day=DAY, hours=DEFAULT_HOURS if hours is None else hours
    )


def recording_fetch(calls, result=None):
    """Stand-in for fetch() that records the kwargs it was called with."""

    def fake(**kw):
        calls.append(kw)
        return day_weather() if result is None else (result or None)

    return fake


# --- anchor hours -----------------------------------------------------------


def test_anchor_hours_are_morning_midday_evening():
    assert weather.ANCHOR_HOURS == (8, 12, 18)


def test_hour_label_reads_as_a_12_hour_clock():
    assert weather.hour_label(8) == "8am"
    assert weather.hour_label(12) == "12pm"
    assert weather.hour_label(18) == "6pm"
    assert weather.hour_label(0) == "12am"


def test_anchors_returns_one_point_per_anchor_hour():
    got = day_weather().anchors()
    assert [p.hour for p in got] == [8, 12, 18]
    assert [p.temp_c for p in got] == [20.0, 22.0, 24.0]
    assert [p.rain_pct for p in got] == [96, 100, 29]


def test_anchors_keeps_the_slot_when_an_hour_is_missing():
    # A hole must stay a hole. Collapsing the list would silently relabel the
    # evening temperature as midday.
    got = day_weather({8: point(8, 20.0, 96), 18: point(18, 24.0, 29)}).anchors()
    assert [p.hour if p else None for p in got] == [8, None, 18]


def test_summary_uses_the_same_anchor_hours():
    # One definition, so the header and the brief prompt cannot disagree.
    text = day_weather().summary()
    assert "08:00" in text and "12:00" in text and "18:00" in text


# --- caching ----------------------------------------------------------------


@pytest.fixture(autouse=True)
def clear_cache():
    weather.clear_cache()
    yield
    weather.clear_cache()


def test_cached_fetch_calls_the_api_once_within_the_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(weather, "fetch", recording_fetch(calls))

    first = weather.fetch_cached(day=DAY)
    second = weather.fetch_cached(day=DAY)

    assert len(calls) == 1
    assert first is second


def test_cached_fetch_refetches_once_the_ttl_expires(monkeypatch):
    calls = []
    monkeypatch.setattr(weather, "fetch", recording_fetch(calls))
    clock = [1000.0]
    monkeypatch.setattr(weather.time, "monotonic", lambda: clock[0])

    weather.fetch_cached(day=DAY)
    clock[0] += weather.CACHE_TTL_SECONDS + 1
    weather.fetch_cached(day=DAY)

    assert len(calls) == 2


def test_a_failed_fetch_is_never_cached(monkeypatch):
    # The API drops connections routinely. Caching a None would turn one dropped
    # handshake into a blank panel for the rest of the TTL.
    calls = []
    monkeypatch.setattr(weather, "fetch", recording_fetch(calls, result=False))

    assert weather.fetch_cached(day=DAY) is None
    assert weather.fetch_cached(day=DAY) is None
    assert len(calls) == 2


def test_separate_cities_do_not_share_a_cache_entry(monkeypatch):
    seen = []

    def fake(location=None, day=None):
        seen.append(location)
        return weather.DayWeather(location=location or "london", day=DAY, hours={})

    monkeypatch.setattr(weather, "fetch", fake)
    weather.fetch_cached(location="london", day=DAY)
    weather.fetch_cached(location="bangkok", day=DAY)

    assert seen == ["london", "bangkok"]


# --- panel route ------------------------------------------------------------


def test_panel_renders_every_anchor(client, monkeypatch):
    monkeypatch.setattr(weather, "fetch", lambda **kw: day_weather())

    body = client.get("/weather/panel").text

    for token in ("8am", "12pm", "6pm", "96%", "100%", "29%", "20", "22", "24"):
        assert token in body, token
    assert "london" in body.lower()


def test_panel_says_nothing_when_the_forecast_is_unavailable(client, monkeypatch):
    monkeypatch.setattr(weather, "fetch", lambda **kw: None)

    body = client.get("/weather/panel").text

    # An empty forecast must not render as zeroes or dashes that read as data.
    assert "0%" not in body
    assert "unavailable" in body.lower()
