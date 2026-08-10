"""Jarvis - local personal OS. Localhost only, never a public port."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app import agent, config, jobs
from app.db import create_db_and_tables, engine, seed_habits
from app.deps import get_session, templates
from app.routers import chat, habits, tasks
from app.security import ALLOWED_HOSTS, block_cross_site

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("jarvis")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_db_and_tables()
    with Session(engine) as session:
        seeded = seed_habits(session)
    if seeded:
        log.info("seeded %d habits", seeded)
    jobs.start()
    # Each agent session owns a CLI subprocess, so idle ones must be closed
    # rather than left to accumulate.
    reaper = asyncio.create_task(agent.reaper_loop())
    yield
    reaper.cancel()
    with suppress(asyncio.CancelledError):
        await reaper
    await agent.shutdown_all()
    jobs.shutdown()


app = FastAPI(
    title="Jarvis", lifespan=lifespan, dependencies=[Depends(block_cross_site)]
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS)
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
app.include_router(habits.router)
app.include_router(tasks.router)
app.include_router(chat.router)


@app.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "habits": [
                habits.build_row(session, h) for h in habits.active_habits(session)
            ],
            "tasks": tasks.open_first(session),
            "today": config.today().strftime("%d.%m.%Y %A"),
            # Both panels render an empty state until their day lands. Passing
            # None rather than fabricated numbers is deliberate: placeholder
            # figures on a dashboard get read as real ones.
            "brief": None,  # Day 3
            "urgent": None,  # Day 3
            "portfolio": None,  # Day 4
        },
    )


@app.get("/career", response_class=HTMLResponse)
def career(request: Request):
    """Placeholder so the nav link resolves. Real panel lands Day 3."""
    return templates.TemplateResponse(request, "career.html", {})
