"""APScheduler jobs, registered from the FastAPI lifespan.

Registration is idempotent on purpose. Under `uvicorn --reload` the lifespan can
run more than once in a process lifetime, and a job registered twice fires twice.
"""

from __future__ import annotations

import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, select

from app import config, db
from app.integrations import notify
from app.services import brief_builder, streaks

log = logging.getLogger("jarvis.jobs")

_scheduler: AsyncIOScheduler | None = None


def heartbeat() -> None:
    """Proves the scheduler is alive. Replaced by real jobs on Day 3."""
    log.info("scheduler heartbeat %s", config.now().isoformat(timespec="seconds"))


def start() -> BackgroundScheduler:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        return _scheduler

    _scheduler = BackgroundScheduler(timezone=config.TIMEZONE)
    _scheduler.add_job(
        heartbeat,
        trigger="interval",
        seconds=30,
        id="heartbeat",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    log.info("scheduler started (tz=%s)", config.TIMEZONE)
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("scheduler stopped")
    _scheduler = None
