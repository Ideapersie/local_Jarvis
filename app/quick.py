"""Quick tier: one request, one response, no agent loop.

Why this exists, now that both tiers are local and free. The saving is no longer
money, it is prefill. The agent tier carries a tool profile the model must read
before it can answer: 1379 tokens for chat_read, and prefill runs at about
14.5 tok/s on this hardware. The snapshot below is 175 tokens and carries no
tools at all, so a question the snapshot answers costs about a second instead of
a minute and a half.

That is why this tier still exists after the migration, and why it must never be
given tools.

Anything needing files, the web, or multi-step reasoning escalates to the agent.
The model decides, because a keyword list cannot tell "how is my streak" from
"how should I prepare for Thursday".
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlmodel import Session, select

from app import config, db
from app.llm import local
from app.llm.base import Message
from app.services import costs, streaks

log = logging.getLogger("jarvis.quick")

ESCALATE = "__ESCALATE__"

SYSTEM = """You are Jarvis, a personal dashboard assistant.

You are given a snapshot of the user's current structured data. Answer from it
directly when it is sufficient.

Set escalate=true when answering properly would need something the snapshot
cannot give you:
- the contents of any file (goals, weaknesses, past briefs, interview notes)
- anything about the wider world, or anything needing a web search
- advice or planning that depends on context beyond these numbers
- writing or changing anything

Set escalate=false for questions the snapshot answers on its own: current
streaks, what is on the to-do list, which applications are at which stage,
skill confidence numbers.

When you answer, be direct and brief. Short sentences. No preamble. Quote the
real numbers. Never invent a value that is not in the snapshot - escalate
instead."""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "escalate": {
            "type": "boolean",
            "description": "True if this needs the full agent (files, web).",
        },
        "answer": {
            "type": "string",
            "description": "The answer, if escalate is false. Empty string otherwise.",
        },
    },
    "required": ["escalate", "answer"],
    "additionalProperties": False,
}


def snapshot() -> str:
    """Compact view of all structured state. A few hundred tokens, not a dump."""
    today = config.today()
    parts: list[str] = [f"Today is {today.isoformat()} ({today.strftime('%A')})."]

    with Session(db.engine) as s:
        habits = s.exec(
            select(db.Habit).where(db.Habit.active == True).order_by(db.Habit.id)
        ).all()
        if habits:
            lines = []
            for h in habits:
                days = streaks.logged_days(s, h.id)
                lines.append(
                    f"  {h.name}: streak {streaks.current_streak_from(days, today)}, "
                    f"best {streaks.best_streak_from(days)}, "
                    f"{'done' if today in days else 'not done'} today"
                )
            parts.append("HABITS:\n" + "\n".join(lines))
        else:
            parts.append("HABITS: none")

        tasks = s.exec(select(db.Task).where(db.Task.done == False)).all()
        parts.append(
            "OPEN TASKS:\n"
            + ("\n".join(f"  {t.title}" for t in tasks) if tasks else "  none")
        )

        apps = s.exec(select(db.Application)).all()
        if apps:
            parts.append(
                "APPLICATIONS:\n"
                + "\n".join(
                    f"  {a.company} - {a.role} [{a.stage}]"
                    + (f", next {a.next_date}" if a.next_date else "")
                    for a in apps
                )
            )
        else:
            parts.append("APPLICATIONS: none")

        skills = s.exec(select(db.Skill)).all()
        if skills:
            parts.append(
                "SKILLS:\n"
                + "\n".join(
                    f"  {sk.name}: {sk.confidence}/5 (target {sk.target})"
                    for sk in skills
                )
            )

    return "\n\n".join(parts)


def try_answer(question: str) -> tuple[str | None, dict[str, Any]]:
    """Answer from the snapshot, or return None to signal escalation.

    Returns (answer_or_None, meta). Any failure returns None so the caller falls
    through to the agent - a broken quick tier must degrade to a slower correct
    answer, never to a wrong one.
    """
    meta: dict[str, Any] = {"tier": "quick", "model": config.LOCAL_MODEL}

    try:
        completion = local.provider.complete(
            messages=[
                Message(
                    "user",
                    f"<snapshot>\n{snapshot()}\n</snapshot>\n\n{question}",
                )
            ],
            system=SYSTEM,
            # Enforced, not requested. Unconstrained this model scored 0/6
            # against RESPONSE_SCHEMA: it answers correctly and then writes
            # markdown with "escalate=false" appended. Every one of those would
            # land in the unparseable branch below and escalate silently, which
            # is a minute and a half of agent prefill for a question the
            # snapshot already answered.
            schema=RESPONSE_SCHEMA,
            max_tokens=1024,
        )
    except Exception:
        log.warning("quick tier call failed, escalating", exc_info=True)
        return None, {**meta, "reason": "error"}

    costs.record(
        "chat_quick",
        "quick",
        config.LOCAL_MODEL,
        0.0,
        auth="local",
        usage=completion.usage.as_dict(),
    )
    meta["cost_usd"] = 0.0

    try:
        parsed = json.loads(completion.text)
    except json.JSONDecodeError:
        log.warning("quick tier returned unparseable output, escalating")
        return None, {**meta, "reason": "unparseable"}

    if parsed.get("escalate") or not parsed.get("answer"):
        return None, {**meta, "reason": "model escalated"}

    return parsed["answer"], meta
