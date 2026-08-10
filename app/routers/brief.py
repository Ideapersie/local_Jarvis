"""Brief panel: view today's brief, trigger a run, dismiss reminders."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app import config, jobs
from app.deps import get_session, templates
from app.integrations import notify
from app.services import brief_builder

log = logging.getLogger("jarvis.brief_router")

router = APIRouter(prefix="/brief", tags=["brief"])


def build_context(session: Session) -> dict:
    """View model for the briefing panel and the urgent card."""
    row = brief_builder.latest(session)
    urgent = [ln for ln in (row.urgent or "").splitlines() if ln.strip()] if row else []
    # Reminders share the urgent card - a 6pm nudge is exactly the sort of thing
    # that belongs there.
    urgent += [r.body for r in notify.unseen(session)]
    return {
        "brief": row,
        "urgent": urgent,
        "jobs": jobs.jobs_status(),
        "telegram": notify.telegram_configured(),
    }


def render(request: Request, session: Session) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/brief_panel.html", build_context(session)
    )


@router.get("/panel", response_class=HTMLResponse)
def panel(request: Request, session: Session = Depends(get_session)):
    return render(request, session)


@router.post("/run", response_class=HTMLResponse)
async def run(request: Request, session: Session = Depends(get_session)):
    """Generate today's brief now. Costs an agent turn."""
    row = await brief_builder.build()
    if row is None:
        raise HTTPException(status_code=500, detail="brief generation failed")
    session.expire_all()
    return render(request, session)


@router.post("/job/{job_id}", response_class=HTMLResponse)
async def trigger(
    job_id: str, request: Request, session: Session = Depends(get_session)
):
    """Fire a scheduled job on demand, so 06:30 behaviour is testable at 14:00."""
    if job_id not in {"morning_brief", "creatine_check", "weekly_reflection"}:
        raise HTTPException(status_code=404, detail="no such job")
    await jobs.run_now(job_id)
    session.expire_all()
    return render(request, session)


@router.post("/reminders/seen", response_class=HTMLResponse)
def dismiss(request: Request, session: Session = Depends(get_session)):
    from sqlmodel import select

    from app import db

    for r in session.exec(select(db.Reminder).where(db.Reminder.seen == False)).all():
        r.seen = True
        session.add(r)
    session.commit()
    return render(request, session)


@router.get("/today", response_class=HTMLResponse)
def read_today(request: Request, session: Session = Depends(get_session)):
    """Full brief text, for reading rather than skimming."""
    row = brief_builder.latest(session)
    if row is None:
        raise HTTPException(status_code=404, detail="no brief today")
    body = config.BRIEFS_DIR / f"{row.day.isoformat()}.md"
    return templates.TemplateResponse(
        request,
        "brief_full.html",
        {"day": row.day, "body": body.read_text(encoding="utf-8")},
    )
