"""Jarvis - local personal OS. Localhost only, never a public port."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from app import config, jobs
from app.db import create_db_and_tables, engine, seed_habits
from app.deps import get_session, templates
from app.routers import habits, tasks

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
    yield
    jobs.shutdown()


app = FastAPI(title="Jarvis", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
app.include_router(habits.router)
app.include_router(tasks.router)


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
            "today": config.today().strftime("%a %d %b"),
        },
    )
