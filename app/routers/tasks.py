"""To-do panel. Lightweight and tied to nothing else."""

from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from app import config
from app.db import Task
from app.deps import get_session, templates

router = APIRouter(prefix="/tasks", tags=["tasks"])


def open_first(session: Session) -> list[Task]:
    return list(session.exec(select(Task).order_by(Task.done, Task.id.desc())).all())


def render_panel(request: Request, session: Session) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/task_panel.html", {"tasks": open_first(session)}
    )


def _get(session: Session, task_id: int) -> Task:
    task = session.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="task not found")
    return task


@router.get("/panel", response_class=HTMLResponse)
def panel(request: Request, session: Session = Depends(get_session)):
    return render_panel(request, session)


@router.post("", response_class=HTMLResponse)
def add(
    request: Request,
    title: str = Form(...),
    due: str = Form(""),
    session: Session = Depends(get_session),
):
    title = title.strip()
    if not title:
        return render_panel(request, session)

    due_date: date | None = None
    if due.strip():
        try:
            due_date = date.fromisoformat(due.strip())
        except ValueError:
            due_date = None

    session.add(Task(title=title, due=due_date, created_at=config.now()))
    session.commit()
    return render_panel(request, session)


@router.post("/{task_id}/toggle", response_class=HTMLResponse)
def toggle(task_id: int, request: Request, session: Session = Depends(get_session)):
    task = _get(session, task_id)
    task.done = not task.done
    session.add(task)
    session.commit()
    return render_panel(request, session)


@router.delete("/{task_id}", response_class=HTMLResponse)
def remove(task_id: int, request: Request, session: Session = Depends(get_session)):
    session.delete(_get(session, task_id))
    session.commit()
    return render_panel(request, session)
