"""Streaks computed from HabitLog, never stored.

Storing a counter means it drifts the first time you backfill a day, so every
number here is derived on read by walking the log.

Two layers on purpose. The `*_from` functions are pure over an already-fetched
set of days, so a caller rendering a panel fetches once and derives four numbers.
The session-taking wrappers are the convenience path for a caller that wants one
number for one habit. Rendering a panel through the wrappers costs one query per
number per habit, which is the N+1 this split exists to avoid.
"""

from __future__ import annotations

from datetime import date, timedelta
from itertools import pairwise

from sqlmodel import Session, select

from app import config
from app.db import HabitLog


def logged_days(session: Session, habit_id: int) -> set[date]:
    """Every day this habit is logged. One query; pass the result around."""
    rows = session.exec(
        select(HabitLog.day).where(
            HabitLog.habit_id == habit_id,
            HabitLog.done == True,
        )
    ).all()
    return set(rows)


# --- pure derivations over an already-fetched day set -----------------------


def current_streak_from(days: set[date], today: date) -> int:
    """Consecutive days ending today, or ending yesterday if today isn't done yet.

    The grace day matters: at 9am a streak you have not yet ticked today is
    still alive. It only breaks once a whole day passes unlogged.
    """
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


def best_streak_from(days: set[date]) -> int:
    """Longest consecutive run anywhere in the log."""
    ordered = sorted(days)
    if not ordered:
        return 0

    best = run = 1
    for prev, cur in pairwise(ordered):
        run = run + 1 if cur - prev == timedelta(days=1) else 1
        best = max(best, run)
    return best


def week_history_from(
    days: set[date], today: date, span: int = 7
) -> list[tuple[date, bool]]:
    """Oldest-first list of (day, done) for the trailing `span` days."""
    return [
        (day, day in days)
        for day in (today - timedelta(days=n) for n in range(span - 1, -1, -1))
    ]


# --- session-taking wrappers -----------------------------------------------


def current_streak(session: Session, habit_id: int, today: date | None = None) -> int:
    return current_streak_from(logged_days(session, habit_id), today or config.today())


def best_streak(session: Session, habit_id: int) -> int:
    return best_streak_from(logged_days(session, habit_id))


def week_history(
    session: Session, habit_id: int, today: date | None = None, span: int = 7
) -> list[tuple[date, bool]]:
    return week_history_from(
        logged_days(session, habit_id), today or config.today(), span
    )


def is_done(session: Session, habit_id: int, day: date | None = None) -> bool:
    return (day or config.today()) in logged_days(session, habit_id)
