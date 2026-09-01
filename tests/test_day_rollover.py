"""The logical day ends at DAY_ROLLOVER_HOUR, not midnight.

The user is usually awake until ~02:00, so a tick at 01:30 belongs to the day
that just ended in every sense except the calendar's. Without this the streak
breaks on a day that was actually kept.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from app import config


@pytest.fixture()
def rollover_at_five(monkeypatch):
    """Pin the hour these tests are about.

    DAY_ROLLOVER_HOUR is read from the environment, so without this the suite
    asserts whatever the machine happens to be configured for - and fails on
    any box that set it, including one that turned the rollover off.
    """
    monkeypatch.setattr(config, "DAY_ROLLOVER_HOUR", 5)


@pytest.mark.parametrize(
    "at, expected",
    [
        (datetime(2026, 8, 31, 9, 0), date(2026, 8, 31)),  # ordinary daytime
        (datetime(2026, 8, 31, 23, 59), date(2026, 8, 31)),  # just before midnight
        (datetime(2026, 9, 1, 0, 1), date(2026, 8, 31)),  # just after midnight
        (datetime(2026, 9, 1, 2, 0), date(2026, 8, 31)),  # the usual bedtime tick
        (datetime(2026, 9, 1, 4, 59), date(2026, 8, 31)),  # last minute of the day
        (datetime(2026, 9, 1, 5, 0), date(2026, 9, 1)),  # rollover
    ],
)
def test_habit_day_rolls_at_five(at, expected, rollover_at_five):
    assert config.habit_day(at) == expected


def test_habit_day_defaults_to_now(monkeypatch, rollover_at_five):
    monkeypatch.setattr(config, "now", lambda: datetime(2026, 9, 1, 1, 0))
    assert config.habit_day() == date(2026, 8, 31)


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 5),
        ("", 5),
        ("0", 0),
        ("4", 4),
        ("11", 11),
        ("12", 5),  # past midday is not a night owl, it is a bug
        ("-1", 5),
        ("five", 5),
    ],
)
def test_rollover_hour_rejects_nonsense(raw, expected):
    assert config.resolve_rollover_hour(raw) == expected


def test_tick_after_midnight_logs_the_day_that_just_ended(
    client, app_session, monkeypatch, rollover_at_five
):
    """The bug this exists for: a 01:00 tick must not start a new day's row."""
    from sqlmodel import select

    from app.db import HabitLog

    monkeypatch.setattr(config, "now", lambda: datetime(2026, 9, 1, 1, 0))
    assert client.post("/habits/1/toggle").status_code == 200

    logged = app_session.exec(select(HabitLog.day).where(HabitLog.habit_id == 1)).all()
    assert logged == [date(2026, 8, 31)]


def test_streak_survives_a_run_of_late_night_ticks(
    client, app_session, monkeypatch, rollover_at_five
):
    """Three days kept, each ticked after midnight, is a streak of three."""
    from app.services import streaks

    for tick_at in (
        datetime(2026, 8, 30, 1, 0),  # closes 29 Aug
        datetime(2026, 8, 31, 1, 30),  # closes 30 Aug
        datetime(2026, 9, 1, 2, 0),  # closes 31 Aug
    ):
        monkeypatch.setattr(config, "now", lambda at=tick_at: at)
        assert client.post("/habits/1/toggle").status_code == 200

    monkeypatch.setattr(config, "now", lambda: datetime(2026, 9, 1, 2, 5))
    days = streaks.logged_days(app_session, 1)
    assert streaks.current_streak_from(days, config.habit_day()) == 3
