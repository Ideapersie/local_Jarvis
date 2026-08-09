"""Streaks computed from HabitLog, never stored.

Storing a counter means it drifts the first time you backfill a day, so every
number here is derived on read by walking the log.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

from sqlmodel import Session, select

from app import config
from app.db import HabitLog


def logged_days(session: Session, habit_id: int) -> set[date]:
    rows = session.exec(
        select(HabitLog.day).where(
            HabitLog.habit_id == habit_id,
            HabitLog.done == True,
        )
    ).all()
    return set(rows)


def current_streak(session: Session, habit_id: int, today: date | None = None) -> int:
    """Consecutive days ending today, or ending yesterday if today isn't done yet.

    The grace day matters: at 9am a streak you have not yet ticked today is
    still alive. It only breaks once a whole day passes unlogged.
    """
    today = today or config.today()
    days = logged_days(session, habit_id)
    if not days:
        return 0

    cursor = today if today in days else today - timedelta(days=1)
    if cursor not in days:
        return 0

    count = 0
    while cursor in days:
        count += 1
        cursor -= timedelta(days=1)
    return count


def best_streak(session: Session, habit_id: int) -> int:
    """Longest consecutive run anywhere in the log."""
    days = sorted(logged_days(session, habit_id))
    if not days:
        return 0

    best = run = 1
    for prev, cur in pairwise(days):
        run = run + 1 if cur - prev == timedelta(days=1) else 1
        best = max(best, run)
    return best


def week_history(
    session: Session, habit_id: int, today: date | None = None, span: int = 7
) -> list[tuple[date, bool]]:
    """Oldest-first list of (day, done) for the trailing `span` days."""
    today = today or config.today()
    days = logged_days(session, habit_id)
    return [
        (day, day in days)
        for day in (today - timedelta(days=n) for n in range(span - 1, -1, -1))
    ]


def is_done(session: Session, habit_id: int, day: date | None = None) -> bool:
    return (day or config.today()) in logged_days(session, habit_id)
