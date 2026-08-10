"""Habit panel. Every response is an HTML partial - no JSON, no client state."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from app import config
from app.db import Habit, HabitLog
from app.deps import get_session, templates
from app.services import streaks

router = APIRouter(prefix="/habits", tags=["habits"])


def build_row(session: Session, habit: Habit) -> dict[str, Any]:
    """View model for one habit row.

    Fetches the habit's logged days once and derives all four numbers from it.
    Going through the session-taking wrappers instead would cost four queries
    per habit.
    """
    today = config.today()
    days = streaks.logged_days(session, habit.id)
    return {
        "id": habit.id,
        "name": habit.name,
        "category": habit.category,
        "reminder_time": habit.reminder_time,
        "done_today": today in days,
        "current": streaks.current_streak_from(days, today),
        "best": streaks.best_streak_from(days),
        "week": [
            {"day": day.isoformat(), "done": done}
            for day, done in streaks.week_history_from(days, today)
        ],
    }


def active_habits(session: Session) -> list[Habit]:
    return list(
        session.exec(select(Habit).where(Habit.active == True).order_by(Habit.id)).all()
    )


def render_panel(request: Request, session: Session) -> HTMLResponse:
    rows = [build_row(session, h) for h in active_habits(session)]
    return templates.TemplateResponse(
        request, "partials/habit_panel.html", {"habits": rows}
    )


def render_row(request: Request, session: Session, habit: Habit) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/habit_row.html", {"h": build_row(session, habit)}
    )


def _get(session: Session, habit_id: int, *, active_only: bool = False) -> Habit:
    habit = session.get(Habit, habit_id)
    if habit is None:
        raise HTTPException(status_code=404, detail="habit not found")
    if active_only and not habit.active:
        # Archiving exists to freeze a habit's history. Accepting a log against
        # an archived habit - from a stale tab or a replayed request - would
        # quietly corrupt the thing the archive was meant to preserve.
        raise HTTPException(status_code=409, detail="habit is archived")
    return habit


@router.get("/panel", response_class=HTMLResponse)
def panel(request: Request, session: Session = Depends(get_session)):
    return render_panel(request, session)


@router.post("/{habit_id}/toggle", response_class=HTMLResponse)
def toggle(habit_id: int, request: Request, session: Session = Depends(get_session)):
    """Tick or untick today. Returns only this habit's row."""
    habit = _get(session, habit_id, active_only=True)
    today = config.today()

    existing = session.exec(
        select(HabitLog).where(HabitLog.habit_id == habit_id, HabitLog.day == today)
    ).first()

    if existing is not None:
        session.delete(existing)
    else:
        session.add(HabitLog(habit_id=habit_id, day=today, done=True))
    session.commit()

    return render_row(request, session, habit)


@router.post("", response_class=HTMLResponse)
def add(
    request: Request,
    name: str = Form(...),
    category: str = Form("personal"),
    reminder_time: str = Form(""),
    session: Session = Depends(get_session),
):
    name = name.strip()
    if not name:
        return render_panel(request, session)

    session.add(
        Habit(
            name=name,
            category=category.strip() or "personal",
            reminder_time=reminder_time.strip() or None,
            created_at=config.today(),
        )
    )
    session.commit()
    return render_panel(request, session)


@router.post("/{habit_id}/archive", response_class=HTMLResponse)
def archive(habit_id: int, request: Request, session: Session = Depends(get_session)):
    """Soft delete. The logs stay, so history and past streaks stay intact."""
    habit = _get(session, habit_id)
    if habit.active:
        # Idempotent: re-archiving must not overwrite the original date.
        habit.active = False
        habit.archived_at = config.today()
        session.add(habit)
        session.commit()
    return render_panel(request, session)
