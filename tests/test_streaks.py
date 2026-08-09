"""The test that proves streaks are computed, not stored.

A stored counter drifts the first time you backfill a day. These assert the
derived value picks the backfill up on the next read.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app.db import Habit, HabitLog
from app.services import streaks

TODAY = date(2026, 8, 9)


@pytest.fixture()
def session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        s.add(Habit(id=1, name="leetcode", category="career", created_at=TODAY))
        s.commit()
        yield s


def log(session: Session, *offsets: int) -> None:
    """Log the habit on `today - offset` for each offset."""
    for off in offsets:
        session.add(HabitLog(habit_id=1, day=TODAY - timedelta(days=off)))
    session.commit()


def test_no_logs_is_zero(session):
    assert streaks.current_streak(session, 1, TODAY) == 0
    assert streaks.best_streak(session, 1) == 0


def test_consecutive_run_ending_today(session):
    log(session, 0, 1, 2)
    assert streaks.current_streak(session, 1, TODAY) == 3


def test_today_unticked_keeps_yesterdays_streak_alive(session):
    # At 9am a streak you have not ticked yet today is not broken.
    log(session, 1, 2, 3)
    assert streaks.current_streak(session, 1, TODAY) == 3


def test_two_missed_days_breaks_the_streak(session):
    log(session, 2, 3, 4)
    assert streaks.current_streak(session, 1, TODAY) == 0


def test_backfill_recomputes_the_streak(session):
    # Days 1, 3, 4, 5 ago logged. The gap at day 2 caps the current streak at 1.
    log(session, 1, 3, 4, 5)
    assert streaks.current_streak(session, 1, TODAY) == 1

    # Backfill the missed day. A stored counter would still say 1.
    log(session, 2)
    assert streaks.current_streak(session, 1, TODAY) == 5


def test_best_streak_finds_the_longest_run_anywhere(session):
    log(session, 10, 11, 12, 13)  # a 4-day run in the past
    log(session, 0, 1)  # a 2-day run ending today
    assert streaks.current_streak(session, 1, TODAY) == 2
    assert streaks.best_streak(session, 1) == 4


def test_week_history_is_seven_days_oldest_first(session):
    log(session, 0, 3)
    week = streaks.week_history(session, 1, TODAY)
    assert len(week) == 7
    assert week[0][0] == TODAY - timedelta(days=6)
    assert week[-1] == (TODAY, True)
    assert [done for _, done in week] == [False, False, False, True, False, False, True]
