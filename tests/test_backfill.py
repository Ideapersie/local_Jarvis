"""Backfilling a missed tick, and the bound that keeps it a correction.

The rollover in config.habit_day covers a tick made at 01:00. It cannot cover
one you remember at 09:00 the next morning, because the day is genuinely over
by then. This is the path for that case - and the reason it is bounded is that
an unbounded one is not a correction, it is a way to invent a streak.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import select

from app import config
from app.db import HabitLog

NOW = datetime(2026, 9, 1, 9, 0)  # morning after, well past the 05:00 rollover


@pytest.fixture()
def frozen(monkeypatch):
    monkeypatch.setattr(config, "now", lambda: NOW)
    return config.habit_day()  # 2026-09-01


def logged(session, habit_id=1):
    return sorted(
        session.exec(select(HabitLog.day).where(HabitLog.habit_id == habit_id)).all()
    )


def test_backfills_yesterday(client, app_session, frozen):
    yesterday = frozen - timedelta(days=1)
    r = client.post(f"/habits/1/toggle?day={yesterday}")
    assert r.status_code == 200
    assert logged(app_session) == [yesterday]


def test_backfill_untick_removes_only_that_day(client, app_session, frozen):
    yesterday = frozen - timedelta(days=1)
    client.post(f"/habits/1/toggle?day={yesterday}")
    client.post("/habits/1/toggle")
    assert logged(app_session) == [yesterday, frozen]

    client.post(f"/habits/1/toggle?day={yesterday}")
    assert logged(app_session) == [frozen]


def test_backfill_repairs_the_streak(client, app_session, frozen):
    from app.services import streaks

    client.post(f"/habits/1/toggle?day={frozen - timedelta(days=2)}")
    client.post("/habits/1/toggle")
    days = streaks.logged_days(app_session, 1)
    assert streaks.current_streak_from(days, frozen) == 1, "gap not yet filled"

    client.post(f"/habits/1/toggle?day={frozen - timedelta(days=1)}")
    days = streaks.logged_days(app_session, 1)
    assert streaks.current_streak_from(days, frozen) == 3


def test_the_oldest_allowed_day_is_accepted(client, app_session, frozen):
    oldest = frozen - timedelta(days=config.HABIT_BACKFILL_DAYS)
    assert client.post(f"/habits/1/toggle?day={oldest}").status_code == 200
    assert logged(app_session) == [oldest]


def test_a_day_past_the_bound_is_rejected(client, app_session, frozen):
    too_old = frozen - timedelta(days=config.HABIT_BACKFILL_DAYS + 1)
    r = client.post(f"/habits/1/toggle?day={too_old}")
    assert r.status_code == 422
    assert logged(app_session) == [], "a rejected backfill must write nothing"


def test_the_future_is_rejected(client, app_session, frozen):
    r = client.post(f"/habits/1/toggle?day={frozen + timedelta(days=1)}")
    assert r.status_code == 422
    assert logged(app_session) == []


def test_a_malformed_day_is_rejected(client, app_session, frozen):
    assert client.post("/habits/1/toggle?day=yesterday").status_code == 422
    assert logged(app_session) == []


def test_backfilling_an_archived_habit_is_rejected(client, app_session, frozen):
    client.post("/habits/1/archive")
    r = client.post(f"/habits/1/toggle?day={frozen - timedelta(days=1)}")
    assert r.status_code == 409
    assert logged(app_session) == []


def test_omitting_the_day_still_means_now(client, app_session, frozen):
    assert client.post("/habits/1/toggle").status_code == 200
    assert logged(app_session) == [frozen]


def test_only_the_backfillable_days_render_as_buttons(client, frozen):
    """The panel must not offer a repair the route would refuse."""
    html = client.get("/habits/panel").text
    editable = [
        frozen - timedelta(days=n)
        for n in range(config.HABIT_BACKFILL_DAYS + 1)
    ]

    for day in editable:
        assert f'hx-post="/habits/1/toggle?day={day}"' in html

    too_old = frozen - timedelta(days=config.HABIT_BACKFILL_DAYS + 1)
    assert f"day={too_old}" not in html
    assert f'title="{too_old}"' in html, "older days still render, just not clickable"


def test_clicking_a_dot_returns_the_updated_row(client, frozen):
    yesterday = frozen - timedelta(days=1)
    html = client.post(f"/habits/1/toggle?day={yesterday}").text
    assert 'aria-pressed="true"' in html
    assert 'id="habit-1"' in html


def test_every_dot_replaces_the_row_rather_than_nesting_inside_it(client, frozen):
    """Without hx-swap the default is innerHTML, which nests a row inside a row.

    It looks like the row drifting left on every click, and it survives until a
    refresh. Cheap to delete by accident, so assert it.
    """
    html = client.get("/habits/panel").text
    buttons = html.count('hx-post="/habits/1/toggle?day=')
    assert buttons == config.HABIT_BACKFILL_DAYS + 1
    assert html.count('hx-swap="outerHTML"') >= buttons
