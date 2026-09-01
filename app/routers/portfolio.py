"""Portfolio panel. HTML partials, like every other panel here.

The analysis endpoint is a POST because it spends money. It is the only route in
the app that does, and making it a GET would mean a refresh or a prefetch could
bill Opus without anyone deciding to.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlmodel import Session

from app.deps import get_session, templates
from app.integrations import trading212
from app.services import portfolio

log = logging.getLogger("jarvis.routers.portfolio")

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


def render(request: Request, session: Session) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "partials/portfolio_panel.html",
        {"portfolio": portfolio.view(session)},
    )


@router.get("/panel", response_class=HTMLResponse)
def panel(request: Request, session: Session = Depends(get_session)):
    """Free: positions and cash from Trading212, plus the last stored note."""
    return render(request, session)


@router.post("/sync", response_class=HTMLResponse)
def sync(request: Request, session: Session = Depends(get_session)):
    """Refresh the Holding rows from Trading212. Costs nothing."""
    written = portfolio.sync(session)
    if written < 0:
        log.warning("portfolio sync failed")
    return render(request, session)


@router.post("/analyse", response_class=HTMLResponse)
def analyse(request: Request, session: Session = Depends(get_session)):
    """Spend one Opus call on the current positions.

    Deliberately explicit. Everything else in Jarvis runs on the local model for
    nothing; this is the one place a click costs money, so it takes a click.
    """
    if not trading212.available():
        log.info("analysis requested with no Trading212 key configured")
        return render(request, session)
    portfolio.analyse(session)
    return render(request, session)
