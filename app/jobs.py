"""Scheduled jobs.

Registration is idempotent on purpose. Under `uvicorn --reload` the lifespan can
run more than once in a process lifetime, and a job registered twice fires twice.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlmodel import Session, select

from app import config, db
from app.integrations import notify
from app.services import brief_builder, streaks

log = logging.getLogger("jarvis.jobs")

_scheduler: AsyncIOScheduler | None = None


# --- 06:30 morning brief ----------------------------------------------------


BRIEF_HOUR = 6
BRIEF_MINUTE = 30


def inbox_triage() -> None:
    """06:25, five minutes ahead of the brief so it reads fresh results."""
    from app.services import triage

    counts = triage.run()
    log.info("06:25 job: triage %s", counts or "(nothing)")


async def morning_brief() -> None:
    log.info("06:30 job: building morning brief")
    row = await brief_builder.build()
    if row is None:
        notify.send("Morning brief failed to generate. Check the log.", kind="brief")
        return
    if row.urgent:
        notify.send(f"Today needs attention:\n{row.urgent}", kind="brief")


def catch_up_due(now: datetime | None = None) -> bool:
    """True if today's brief was missed and is still worth building.

    APScheduler is in-process: a cron job for 06:30 does not fire at all if the
    machine was off at 06:30, and nothing retries it. Autostart at login only
    helps when login happens before the scheduled time - boot the laptop at 09:00
    and you would silently get no brief that day. This is the check that closes
    that hole.
    """
    now = now or config.now()
    scheduled = now.replace(
        hour=BRIEF_HOUR, minute=BRIEF_MINUTE, second=0, microsecond=0
    )
    if now < scheduled:
        return False  # 06:30 has not passed yet; the cron job will handle it.

    with Session(db.engine) as s:
        return brief_builder.latest(s, now.date()) is None


async def catch_up() -> None:
    """Build a missed brief once, on startup. Never on a schedule."""
    if not catch_up_due():
        return
    log.info("catch-up: today's brief was missed, building it now")
    await morning_brief()


# --- 18:00 creatine check ---------------------------------------------------


def creatine_due() -> bool:
    """True if the creatine habit exists, is active, and is not done today.

    This is a boolean, so Python evaluates it. Asking a model to decide whether
    a row exists is both more expensive and less reliable than a query.
    """
    today = config.habit_day()
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

    _scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)
    common = dict(replace_existing=True, max_instances=1, coalesce=True)

    _scheduler.add_job(
        inbox_triage, "cron", hour=6, minute=25, id="inbox_triage", **common
    )
    _scheduler.add_job(
        morning_brief,
        "cron",
        hour=BRIEF_HOUR,
        minute=BRIEF_MINUTE,
        id="morning_brief",
        **common,
    )
    _scheduler.add_job(
        creatine_check, "cron", hour=18, minute=0, id="creatine_check", **common
    )
    _scheduler.add_job(
        weekly_reflection,
        "cron",
        day_of_week="sun",
        hour=19,
        minute=0,
        id="weekly_reflection",
        **common,
    )

    _scheduler.start()
    log.info(
        "scheduler started (tz=%s): %s",
        config.TIMEZONE,
        ", ".join(j.id for j in _scheduler.get_jobs()),
    )
    return _scheduler


def shutdown() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
        log.info("scheduler stopped")
    _scheduler = None


def jobs_status() -> list[dict]:
    if _scheduler is None or not _scheduler.running:
        return []
    return [
        {
            "id": j.id,
            "next": j.next_run_time.strftime("%a %d %b %H:%M")
            if j.next_run_time
            else None,
        }
        for j in _scheduler.get_jobs()
    ]


async def run_now(job_id: str) -> None:
    """Fire a job immediately, for testing. Sync jobs go to a thread."""
    targets = {
        "inbox_triage": inbox_triage,
        "morning_brief": morning_brief,
        "creatine_check": creatine_check,
        "weekly_reflection": weekly_reflection,
    }
    fn = targets[job_id]
    if asyncio.iscoroutinefunction(fn):
        await fn()
    else:
        await asyncio.to_thread(fn)
