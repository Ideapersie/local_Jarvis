"""Career tracker: applications by stage, prep notes, skills against target.

Grouped by stage rather than kanban. Seven stages in columns would be ~180px
each and would need drag-and-drop, which means client-side state - the one thing
the rest of this app deliberately does not have.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session, select

from app import config, db
from app.deps import get_session, templates

router = APIRouter(prefix="/career", tags=["career"])

# Pipeline order. Rejected sits last because it is history, not pipeline.
STAGES = ["applied", "oa", "phone", "technical", "final", "offer", "rejected"]
LIVE_STAGES = [s for s in STAGES if s != "rejected"]

STAGE_LABELS = {
    "applied": "Applied",
    "oa": "Online assessment",
    "phone": "Phone screen",
    "technical": "Technical",
    "final": "Final round",
    "offer": "Offer",
    "rejected": "Rejected",
}

PREP_KINDS = ["topic_to_learn", "weakness", "question_asked", "outcome"]

# An interview inside this many days gets the prep button.
PREP_WINDOW_DAYS = 7


def _parse_date(raw: str) -> date | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def build_application(session: Session, app: db.Application) -> dict[str, Any]:
    """View model for one application, including its unresolved prep notes."""
    today = config.today()
    days_away = (app.next_date - today).days if app.next_date else None
    notes = session.exec(
        select(db.PrepNote)
        .where(db.PrepNote.application_id == app.id)
        .order_by(db.PrepNote.resolved, db.PrepNote.id)
    ).all()

    return {
        "id": app.id,
        "company": app.company,
        "role": app.role,
        "stage": app.stage,
        "next_date": app.next_date,
        "days_away": days_away,
        "source": app.source,
        # Prep is offered for imminent interviews only. A screening call in six
        # weeks does not need a plan today, and generating one costs a turn.
        "prep_ready": (
            days_away is not None
            and 0 <= days_away <= PREP_WINDOW_DAYS
            and app.stage not in {"rejected", "offer"}
        ),
        "overdue": days_away is not None and days_away < 0,
        "notes": [
            {
                "id": n.id,
                "kind": n.kind,
                "body": n.body,
                "resolved": n.resolved,
            }
            for n in notes
        ],
        "open_notes": sum(1 for n in notes if not n.resolved),
    }


def build_context(session: Session) -> dict[str, Any]:
    apps = session.exec(
        select(db.Application).order_by(db.Application.next_date, db.Application.id)
    ).all()
    rows = [build_application(session, a) for a in apps]

    grouped = []
    for stage in STAGES:
        in_stage = [r for r in rows if r["stage"] == stage]
        # Empty stages are dropped rather than shown as blank headings - an
        # empty "Offer" section every day is discouraging and uninformative.
        if in_stage:
            grouped.append(
                {"stage": stage, "label": STAGE_LABELS[stage], "apps": in_stage}
            )

    skills = session.exec(select(db.Skill).order_by(db.Skill.name)).all()

    return {
        "grouped": grouped,
        "total": len(rows),
        "live": sum(1 for r in rows if r["stage"] != "rejected"),
        "stages": STAGES,
        "stage_labels": STAGE_LABELS,
        "prep_kinds": PREP_KINDS,
        "skills": [
            {
                "id": s.id,
                "name": s.name,
                "confidence": s.confidence,
                "target": s.target,
                "gap": max(0, s.target - s.confidence),
                "last_touched": s.last_touched,
            }
            for s in skills
        ],
    }


def render(request: Request, session: Session) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "partials/career_body.html", build_context(session)
    )


def _get_app(session: Session, app_id: int) -> db.Application:
    app = session.get(db.Application, app_id)
    if app is None:
        raise HTTPException(status_code=404, detail="application not found")
    return app


# --- page and panel ---------------------------------------------------------


@router.get("/panel", response_class=HTMLResponse)
def panel(request: Request, session: Session = Depends(get_session)):
    return render(request, session)


# --- applications -----------------------------------------------------------


@router.post("/applications", response_class=HTMLResponse)
def add_application(
    request: Request,
    company: str = Form(...),
    role: str = Form(...),
    stage: str = Form("applied"),
    next_date: str = Form(""),
    source: str = Form(""),
    session: Session = Depends(get_session),
):
    company, role = company.strip(), role.strip()
    if not company or not role:
        return render(request, session)

    stage = stage.strip().lower()
    if stage not in STAGES:
        stage = "applied"

    session.add(
        db.Application(
            company=company,
            role=role,
            stage=stage,
            next_date=_parse_date(next_date),
            source=source.strip() or None,
            updated_at=config.now(),
        )
    )
    session.commit()
    return render(request, session)


@router.post("/applications/{app_id}/stage", response_class=HTMLResponse)
def set_stage(
    app_id: int,
    request: Request,
    stage: str = Form(...),
    session: Session = Depends(get_session),
):
    stage = stage.strip().lower()
    if stage not in STAGES:
        raise HTTPException(status_code=400, detail=f"unknown stage {stage!r}")

    app = _get_app(session, app_id)
    app.stage = stage
    # A rejection has no next date. Leaving a stale one behind would keep the
    # application showing in the brief's fourteen-day window forever.
    if stage == "rejected":
        app.next_date = None
    app.updated_at = config.now()
    session.add(app)
    session.commit()
    return render(request, session)


@router.post("/applications/{app_id}/date", response_class=HTMLResponse)
def set_date(
    app_id: int,
    request: Request,
    next_date: str = Form(""),
    session: Session = Depends(get_session),
):
    app = _get_app(session, app_id)
    app.next_date = _parse_date(next_date)
    app.updated_at = config.now()
    session.add(app)
    session.commit()
    return render(request, session)


@router.delete("/applications/{app_id}", response_class=HTMLResponse)
def delete_application(
    app_id: int, request: Request, session: Session = Depends(get_session)
):
    app = _get_app(session, app_id)
    # Prep notes are meaningless without their application, and SQLite will not
    # cascade for us.
    for note in session.exec(
        select(db.PrepNote).where(db.PrepNote.application_id == app_id)
    ).all():
        session.delete(note)
    session.delete(app)
    session.commit()
    return render(request, session)


PREP_PROMPT = """Use the interview-prep skill for this interview.

Application id: {app_id}
Company: {company}
Role: {role}
Stage: {stage}
Date: {next_date} ({days_away} days away)

Read its existing prep notes before planning so you do not repeat them, then
record what to work on with add_prep_note."""


@router.post("/applications/{app_id}/prep", response_class=HTMLResponse)
async def run_prep(
    app_id: int, request: Request, session: Session = Depends(get_session)
):
    """Run the interview-prep skill against the local model."""
    from app import loop

    app = _get_app(session, app_id)
    if app.next_date is None:
        raise HTTPException(
            status_code=400, detail="application has no date to prepare for"
        )

    prompt = PREP_PROMPT.format(
        app_id=app.id,
        company=app.company,
        role=app.role,
        stage=app.stage,
        next_date=app.next_date.isoformat(),
        days_away=(app.next_date - config.today()).days,
    )

    # The prep profile deliberately omits get_applications: every detail it
    # would return is already in the prompt above, and with it in scope the
    # model routes write intents to it instead of add_prep_note.
    await loop.run_to_text(
        f"prep-{app_id}",
        prompt,
        profile="prep",
        skill_name="interview-prep",
        kind="interview_prep",
        think=True,
        max_tokens=4096,
    )

    session.expire_all()
    return render(request, session)


# --- prep notes -------------------------------------------------------------


@router.post("/applications/{app_id}/notes", response_class=HTMLResponse)
def add_note(
    app_id: int,
    request: Request,
    body: str = Form(...),
    kind: str = Form("topic_to_learn"),
    session: Session = Depends(get_session),
):
    _get_app(session, app_id)
    body = body.strip()
    if not body:
        return render(request, session)

    kind = kind.strip().lower()
    if kind not in PREP_KINDS:
        kind = "topic_to_learn"

    session.add(
        db.PrepNote(
            application_id=app_id, kind=kind, body=body, created_at=config.now()
        )
    )
    session.commit()
    return render(request, session)


@router.post("/notes/{note_id}/toggle", response_class=HTMLResponse)
def toggle_note(
    note_id: int, request: Request, session: Session = Depends(get_session)
):
    note = session.get(db.PrepNote, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="prep note not found")
    note.resolved = not note.resolved
    session.add(note)
    session.commit()
    return render(request, session)


@router.delete("/notes/{note_id}", response_class=HTMLResponse)
def delete_note(
    note_id: int, request: Request, session: Session = Depends(get_session)
):
    note = session.get(db.PrepNote, note_id)
    if note is None:
        raise HTTPException(status_code=404, detail="prep note not found")
    session.delete(note)
    session.commit()
    return render(request, session)


# --- skills -----------------------------------------------------------------


@router.post("/skills", response_class=HTMLResponse)
def add_skill(
    request: Request,
    name: str = Form(...),
    confidence: int = Form(1),
    target: int = Form(3),
    session: Session = Depends(get_session),
):
    name = name.strip()
    if not name:
        return render(request, session)

    session.add(
        db.Skill(
            name=name,
            confidence=max(1, min(5, confidence)),
            target=max(1, min(5, target)),
            last_touched=config.today(),
        )
    )
    session.commit()
    return render(request, session)


@router.post("/skills/{skill_id}", response_class=HTMLResponse)
def update_skill(
    skill_id: int,
    request: Request,
    confidence: int = Form(...),
    session: Session = Depends(get_session),
):
    skill = session.get(db.Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    skill.confidence = max(1, min(5, confidence))
    skill.last_touched = config.today()
    session.add(skill)
    session.commit()
    return render(request, session)


@router.delete("/skills/{skill_id}", response_class=HTMLResponse)
def delete_skill(
    skill_id: int, request: Request, session: Session = Depends(get_session)
):
    skill = session.get(db.Skill, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    session.delete(skill)
    session.commit()
    return render(request, session)
