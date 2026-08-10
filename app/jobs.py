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


# --- 06:30 morning brief ----------------------------------------------------


async def morning_brief() -> None:
    log.info("06:30 job: building morning brief")
    row = await brief_builder.build()
    if row is None:
        notify.send("Morning brief failed to generate. Check the log.", kind="brief")
        return
    if row.urgent:
        notify.send(f"Today needs attention:\n{row.urgent}", kind="brief")


# --- 18:00 creatine check ---------------------------------------------------


def creatine_due() -> bool:
    """True if the creatine habit exists, is active, and is not done today.

    This is a boolean, so Python evaluates it. Asking a model to decide whether
    a row exists is both more expensive and less reliable than a query.
    """
    today = config.today()
    with Session(db.engine) as s:
        habit = next(
            (
                h
                for h in s.exec(select(db.Habit).where(db.Habit.active == True)).all()
                if h.name.strip().lower() == "creatine"
            ),
            None,
        )
        if habit is None:
            return False
        return today not in streaks.logged_days(s, habit.id)


def creatine_check() -> None:
    """Send exactly one message, and only if it is actually outstanding."""
    if not creatine_due():
        log.info("18:00 job: creatine already done, sending nothing")
        return
    via = notify.send("Creatine not logged yet today.", kind="creatine")
    log.info("18:00 job: reminder sent via %s", via)


# --- Sunday 19:00 reflection ------------------------------------------------


async def weekly_reflection() -> None:
    """Rewrites goals.md from the week's real data. Agent tier, once a week."""
    from claude_agent_sdk import ResultMessage

    from app import agent
    from app.services import costs

    log.info("Sunday job: weekly reflection")
    prompt = (
        "Review the past week: read brain/goals.md, brain/weaknesses.md, and the "
        "last seven files in briefs/. Then rewrite the 'This month' section of "
        "brain/goals.md to reflect what actually happened. Keep every other "
        "section intact. Do not invent progress that the briefs do not show. "
        "Reply with two sentences on what changed."
    )
    try:
        entry = await agent.get_entry("weekly-reflection")
        async with entry.lock:
            await entry.client.query(prompt)
            async for msg in entry.client.receive_response():
                if isinstance(msg, ResultMessage):
                    costs.record(
                        "reflection",
                        "agent",
                        config.MODEL_AGENT,
                        msg.total_cost_usd,
                        auth=agent.active_auth(),
                        usage=msg.usage or {},
                    )
                    break
    except Exception:
        log.exception("weekly reflection failed")


# --- registration -----------------------------------------------------------


def start() -> AsyncIOScheduler:
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
