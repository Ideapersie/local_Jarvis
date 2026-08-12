"""Catch-up for a brief the in-process scheduler could not fire.

APScheduler does not retry a cron job the machine slept through, so a laptop
that was off at 06:30 simply never gets a brief. These pin the decision of when
to build one late - and, just as importantly, when not to.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlmodel import Session

from app import config, db, jobs
from app.services import brief_builder

DAY = date(2026, 8, 12)


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(DAY.year, DAY.month, DAY.day, hour, minute)


def write_brief(session: Session, day: date = DAY) -> None:
    session.add(
        db.Brief(
            day=day,
            summary="already built",
            path=f"briefs/{day.isoformat()}.md",
            created_at=datetime.now(),
        )
    )
    session.commit()


@pytest.fixture()
def store(test_engine):
    with Session(test_engine) as s:
        yield s


# --- when to catch up -------------------------------------------------------


def test_no_catch_up_before_the_scheduled_time(store):
    """At 05:00 the cron job still has its chance. Do not pre-empt it."""
    assert jobs.catch_up_due(at(5, 0)) is False


def test_no_catch_up_one_minute_early(store):
    assert jobs.catch_up_due(at(6, 29)) is False


def test_catches_up_after_the_scheduled_time(store):
    """Booted at 09:00 with nothing for today - this is the case that matters."""
    assert jobs.catch_up_due(at(9, 0)) is True


def test_catches_up_exactly_at_the_scheduled_minute(store):
    assert jobs.catch_up_due(at(6, 30)) is True


def test_no_catch_up_when_today_already_has_a_brief(store):
    write_brief(store)
    assert jobs.catch_up_due(at(9, 0)) is False


def test_yesterdays_brief_does_not_count_as_today(store):
    """The gap this closes is a missed day; a stale brief must not mask it."""
    write_brief(store, DAY - timedelta(days=1))
    assert jobs.catch_up_due(at(9, 0)) is True


def test_a_late_evening_start_still_catches_up(store):
    assert jobs.catch_up_due(at(23, 30)) is True


# --- not building twice -----------------------------------------------------


@pytest.mark.asyncio
async def test_catch_up_does_nothing_when_not_due(store, monkeypatch):
    """A restart at 10:00 after the brief already ran must not rebuild it -
    that would cost a second agent turn and overwrite the morning's file."""
    write_brief(store)
    called = False

    async def spy():
        nonlocal called
        called = True

    monkeypatch.setattr(jobs, "morning_brief", spy)
    monkeypatch.setattr(config, "now", lambda: at(10, 0))

    await jobs.catch_up()
    assert called is False


@pytest.mark.asyncio
async def test_catch_up_builds_once_when_due(store, monkeypatch):
    calls = 0

    async def spy():
        nonlocal calls
        calls += 1

    monkeypatch.setattr(jobs, "morning_brief", spy)
    monkeypatch.setattr(config, "now", lambda: at(9, 0))

    await jobs.catch_up()
    assert calls == 1


# --- panel state ------------------------------------------------------------


def test_most_recent_returns_the_newest_day(store):
    write_brief(store, DAY - timedelta(days=3))
    write_brief(store, DAY - timedelta(days=1))
    assert brief_builder.most_recent(store).day == DAY - timedelta(days=1)


def test_most_recent_is_none_on_an_empty_database(store):
    assert brief_builder.most_recent(store) is None
