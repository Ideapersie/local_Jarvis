"""Assembles the morning brief.

The split that matters: every fact is gathered here, in plain Python, and the
agent is only asked for judgement. Deterministic data fetched in code, judgement
delegated to the model - never ask the agent to fetch the weather or compute a
streak, because a wrong number in a brief you skim is a wrong number you act on.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock
from sqlmodel import Session, select

from app import config, db
from app.integrations import gcal
from app.integrations import weather as weather_mod
from app.services import costs, streaks, triage

log = logging.getLogger("jarvis.brief")

UPCOMING_DAYS = 14


def gather(day: date | None = None) -> dict:
    """Every deterministic input the brief needs. No model calls in here."""
    day = day or config.today()
    out: dict = {"day": day}

    with Session(db.engine) as s:
        habits = []
        for h in s.exec(
            select(db.Habit).where(db.Habit.active == True).order_by(db.Habit.id)
        ).all():
            days = streaks.logged_days(s, h.id)
            habits.append(
                {
                    "name": h.name,
                    "category": h.category,
                    "current": streaks.current_streak_from(days, day),
                    "best": streaks.best_streak_from(days),
                    "done_yesterday": (day - timedelta(days=1)) in days,
                    "reminder_time": h.reminder_time,
                }
            )
        out["habits"] = habits

        out["tasks"] = [
            {"title": t.title, "due": t.due.isoformat() if t.due else None}
            for t in s.exec(select(db.Task).where(db.Task.done == False)).all()
        ]

        horizon = day + timedelta(days=UPCOMING_DAYS)
        out["applications"] = [
            {
                "company": a.company,
                "role": a.role,
                "stage": a.stage,
                "next_date": a.next_date.isoformat() if a.next_date else None,
                "days_away": (a.next_date - day).days if a.next_date else None,
            }
            for a in s.exec(
                select(db.Application)
                .where(db.Application.next_date != None)
                .where(db.Application.next_date >= day)
                .where(db.Application.next_date <= horizon)
                .order_by(db.Application.next_date)
            ).all()
        ]

    # Calendar first: the day's events decide which location the weather is for
    # and which hours are worth bracketing.
    fetched = gcal.upcoming(days=7, start=day) if gcal.available() else None
    events = fetched or []
    todays = gcal.today(events, day)
    # "Connected" has to mean the call came back, not that credentials.json is
    # on disk. gcal.available() only checks the latter, so a revoked token or a
    # missing client library used to render as "the calendar was checked and is
    # clear" - a fact nobody established.
    out["calendar_connected"] = fetched is not None
    out["events_today"] = [str(e) for e in todays]
    out["events_week"] = [
        f"{e.day.strftime('%a %d %b')}: {e}" for e in events if e.day != day
    ][:8]

    # Location can be overridden by an event that names the other city.
    location_hint = next(
        (
            e.location
            for e in todays
            if e.location
            and weather_mod.resolve_location(e.location)
            != weather_mod.resolve_location(None)
        ),
        None,
    )
    fc = weather_mod.fetch(location=location_hint, day=day)
    out["weather"] = fc.summary() if fc else None
    out["location"] = fc.location if fc else None

    # The spec's two-lookup pattern, now that there are events to key off:
    # one hour before and two hours after, passed as plain numbers. The model is
    # never asked to reason about a time window from a raw forecast blob.
    brackets = []
    if fc:
        for event in todays:
            if not event.outdoors or event.hour is None:
                continue
            before, after = fc.around(event.hour)
            if before and after:
                brackets.append(
                    f"{event.summary} at {event.hour:02d}:00 - "
                    f"before: {before}; after: {after}"
                )
    out["weather_brackets"] = brackets

    with Session(db.engine) as s:
        out["triage"] = triage.summary(s)
        out["reply_needed"] = [
            {"sender": t.sender, "subject": t.subject, "why": t.why}
            for t in triage.pending(s, limit=5)
        ]
    out["email_connected"] = triage.available()

    # Recent briefs let the agent spot repetition. Names only - it greps the
    # bodies itself if it wants them, rather than us paying to inline seven days.
    out["recent_briefs"] = sorted(p.name for p in config.BRIEFS_DIR.glob("*.md"))[-7:]

    return out


def render_facts(facts: dict) -> str:
    """The gathered facts as a compact block for the prompt."""
    day: date = facts["day"]
    lines = [f"DATE: {day.isoformat()} ({day.strftime('%A')})"]

    if facts.get("weather"):
        lines.append(f"WEATHER ({facts['location']}): {facts['weather']}")
    else:
        lines.append("WEATHER: unavailable - do not mention weather.")

    if facts["habits"]:
        lines.append("HABITS:")
        for h in facts["habits"]:
            miss = "" if h["done_yesterday"] else "  [missed yesterday]"
            rem = f", reminder {h['reminder_time']}" if h["reminder_time"] else ""
            lines.append(
                f"  {h['name']}: streak {h['current']}, best {h['best']}{rem}{miss}"
            )
    else:
        lines.append("HABITS: none")

    lines.append("OPEN TASKS:")
    lines.extend(
        [
            f"  {t['title']}" + (f" (due {t['due']})" if t["due"] else "")
            for t in facts["tasks"]
        ]
        or ["  none"]
    )

    if facts["applications"]:
        lines.append(f"UPCOMING (next {UPCOMING_DAYS} days):")
        for a in facts["applications"]:
            lines.append(
                f"  {a['company']} - {a['role']} [{a['stage']}]"
                f" in {a['days_away']} days ({a['next_date']})"
            )
    else:
        lines.append("UPCOMING: no applications with dates. Do not invent interviews.")

    if facts["recent_briefs"]:
        lines.append("RECENT BRIEFS: " + ", ".join(facts["recent_briefs"]))

    # "Not connected" and "connected but empty" mean different things, and the
    # brief must not blur them. Saying "nothing on the calendar" when there is
    # no calendar reads as a checked state and is a fabricated observation.
    if facts.get("calendar_connected"):
        if facts["events_today"]:
            lines.append("TODAY'S EVENTS:")
            lines.extend(f"  {e}" for e in facts["events_today"])
        else:
            lines.append("TODAY'S EVENTS: none. The calendar was checked and is clear.")
        if facts["events_week"]:
            lines.append("LATER THIS WEEK:")
            lines.extend(f"  {e}" for e in facts["events_week"])
    else:
        lines.append(
            "CALENDAR: not connected. Do not mention meetings or say the day is "
            "clear - that would imply you checked."
        )

    if facts.get("weather_brackets"):
        lines.append("WEATHER AROUND OUTDOOR EVENTS:")
        lines.extend(f"  {b}" for b in facts["weather_brackets"])

    if facts.get("email_connected"):
        counts = facts.get("triage") or {}
        if counts:
            lines.append(
                "INBOX (unhandled): "
                + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
            )
        else:
            lines.append("INBOX: nothing unhandled.")
        if facts.get("reply_needed"):
            lines.append("NEEDS A REPLY:")
            for item in facts["reply_needed"]:
                why = f" - {item['why']}" if item["why"] else ""
                lines.append(f"  {item['sender']}: {item['subject']}{why}")
    else:
        lines.append(
            "EMAIL: not connected. Do not mention email or say the inbox is "
            "clear - that would imply you checked."
        )

    return "\n".join(lines)


PROMPT = """Use the brief-writer skill to write today's morning brief.

These facts are already gathered and correct. Do not recompute or re-fetch them:

<facts>
{facts}
</facts>

Read brain/goals.md and brain/weaknesses.md before writing the Focus section.
Write the brief to {path}, then reply with a two-sentence summary only."""


async def build(day: date | None = None) -> db.Brief | None:
    """Gather, ask the agent for judgement, persist. Returns the Brief row."""
    from app import agent

    day = day or config.today()
    config.BRIEFS_DIR.mkdir(parents=True, exist_ok=True)
    path = config.BRIEFS_DIR / f"{day.isoformat()}.md"

    facts = gather(day)
    prompt = PROMPT.format(facts=render_facts(facts), path=path)

    summary_parts: list[str] = []
    try:
        entry = await agent.get_entry(f"brief-{day.isoformat()}")
        async with entry.lock:
            await entry.client.query(prompt)
            async for msg in entry.client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            summary_parts.append(block.text)
                elif isinstance(msg, ResultMessage):
                    costs.record(
                        "brief",
                        "agent",
                        config.MODEL_AGENT,
                        msg.total_cost_usd,
                        auth=agent.active_auth(),
                        usage=msg.usage or {},
                    )
                    log.info(
                        "brief built for %s: cost=$%.4f turns=%d",
                        day,
                        msg.total_cost_usd or 0.0,
                        msg.num_turns,
                    )
                    break
    except Exception:
        log.exception("brief generation failed for %s", day)
        return None

    if not path.exists():
        log.error("agent did not write %s - not recording a brief", path)
        return None

    body = path.read_text(encoding="utf-8")
    # Only the final block. Joining every TextBlock concatenated the agent's
    # own narration ("Now invoking the brief-writer skill...") onto the answer,
    # and that string is what the dashboard panel renders.
    summary = summary_parts[-1].strip() if summary_parts else ""
    return _persist(day, path, summary, _extract_urgent(body))


def _extract_urgent(body: str) -> str | None:
    """Pull the Urgent section out for the dashboard card."""
    lower = body.lower()
    idx = lower.find("## urgent")
    if idx == -1:
        return None
    section = body[idx:].split("\n", 1)[-1]
    # Stop at the next heading, if any.
    for line in section.splitlines():
        if line.startswith("## "):
            section = section.split(line)[0]
            break
    items = [
        ln.lstrip("-*# ").strip()
        for ln in section.splitlines()
        if ln.strip() and not ln.startswith("#")
    ]
    return "\n".join(items) or None


def _persist(day: date, path, summary: str, urgent: str | None) -> db.Brief:
    with Session(db.engine) as s:
        existing = s.exec(select(db.Brief).where(db.Brief.day == day)).first()
        if existing:
            existing.summary = summary
            existing.urgent = urgent
            existing.path = str(path)
            row = existing
        else:
            row = db.Brief(
                day=day,
                summary=summary,
                path=str(path),
                urgent=urgent,
                created_at=config.now(),
            )
        s.add(row)
        s.commit()
        s.refresh(row)
        return row


def latest(session: Session, day: date | None = None) -> db.Brief | None:
    return session.exec(
        select(db.Brief).where(db.Brief.day == (day or config.today()))
    ).first()


def most_recent(session: Session) -> db.Brief | None:
    """The newest brief of any day, for showing how long it has been."""
    return session.exec(
        select(db.Brief).order_by(db.Brief.day.desc()).limit(1)
    ).first()
