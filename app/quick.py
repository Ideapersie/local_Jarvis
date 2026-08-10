"""Quick tier: one request, one response, no agent loop.

Why this exists. The agent tier carries the whole Claude Code harness - tools,
skills, project config - which measured at ~25k tokens of context and ~$0.095
before the question is even read, re-sent on every internal turn. A real chat
turn came to $0.17-0.20 and eight turns.

Most chat questions are a database lookup. The dashboard's entire structured
state fits in a few hundred tokens, so Haiku can answer from a prefetched
snapshot for roughly $0.002 - about ninety times cheaper, and faster, because
there is no loop.

Anything needing files, the web, or multi-step reasoning escalates to the agent.
The model decides, because a keyword list cannot tell "how is my streak" from
"how should I prepare for Thursday".
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic
from sqlmodel import Session, select

from app import config, db
from app.services import costs, streaks

log = logging.getLogger("jarvis.quick")

_client: anthropic.Anthropic | None = None

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


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


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

    Returns (answer_or_None, meta). Any failure returns None so the caller
    falls through to the agent - a broken quick tier must degrade to a slower
    correct answer, never to a wrong one.
    """
    meta: dict[str, Any] = {"tier": "quick", "model": config.MODEL_QUICK}

    if not config.ANTHROPIC_API_KEY:
        # Quick tier is a direct API call and has no subscription path.
        return None, {**meta, "reason": "no api key"}

    try:
        resp = client().messages.create(
            model=config.MODEL_QUICK,
            max_tokens=1024,
            system=SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"<snapshot>\n{snapshot()}\n</snapshot>\n\n{question}",
                }
            ],
            output_config={
                "format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}
            },
        )
    except Exception:
        log.warning("quick tier call failed, escalating", exc_info=True)
        return None, {**meta, "reason": "error"}

    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "cache_read_input_tokens": getattr(resp.usage, "cache_read_input_tokens", 0),
    }
    cost = _price(resp.usage)
    costs.record(
        "chat_quick", "quick", config.MODEL_QUICK, cost, auth="api_key", usage=usage
    )
    meta["cost_usd"] = cost

    try:
        text = next(b.text for b in resp.content if b.type == "text")
        parsed = json.loads(text)
    except (StopIteration, json.JSONDecodeError):
        log.warning("quick tier returned unparseable output, escalating")
        return None, {**meta, "reason": "unparseable"}

    if parsed.get("escalate") or not parsed.get("answer"):
        return None, {**meta, "reason": "model escalated"}

    return parsed["answer"], meta


# Haiku 4.5 list pricing, USD per million tokens.
_IN_PER_MTOK = 1.00
_OUT_PER_MTOK = 5.00


def _price(usage: Any) -> float:
    return (
        usage.input_tokens * _IN_PER_MTOK + usage.output_tokens * _OUT_PER_MTOK
    ) / 1_000_000
