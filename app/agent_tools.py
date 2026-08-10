"""Read-only database access for the agent, as in-process MCP tools.

Read-only on purpose. The agent gets writes per table on Day 3, once these reads
are proven correct against real data - a hallucinated write to jarvis.db has no
diff to review, unlike a write to brain/.

Each handler returns compact prose rather than a table dump. The agent reads
these results as context, and a wall of columns costs tokens without adding
anything it can act on.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from claude_agent_sdk import ToolAnnotations, create_sdk_mcp_server, tool
from sqlmodel import Session, select

from app import config, db
from app.services import streaks

READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


def _session() -> Session:
    """Fresh session per call.

    Reads app.db.engine at call time rather than binding it at import, so the
    test fixture's monkeypatch actually takes effect.
    """
    return Session(db.engine)


def _text(body: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": body}]}


def _error(body: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": body}], "is_error": True}


# --- habits -----------------------------------------------------------------


@tool(
    "get_habits",
    "List the user's active habits with their current streak, best streak, and "
    "whether each is done today. Use this for any question about habits, "
    "streaks, consistency, or what the user has kept up with.",
    {},
    annotations=READ_ONLY,
)
async def get_habits(args: dict[str, Any]) -> dict[str, Any]:
    today = config.today()
    with _session() as s:
        habits = s.exec(
            select(db.Habit).where(db.Habit.active == True).order_by(db.Habit.id)
        ).all()
        if not habits:
            return _text("No active habits.")

        lines = []
        for h in habits:
            days = streaks.logged_days(s, h.id)
            lines.append(
                f"- {h.name} ({h.category}): "
                f"current streak {streaks.current_streak_from(days, today)}, "
                f"best {streaks.best_streak_from(days)}, "
                f"{'done' if today in days else 'not done'} today"
                + (f", reminder {h.reminder_time}" if h.reminder_time else "")
            )
    return _text(f"Active habits as of {today}:\n" + "\n".join(lines))


@tool(
    "get_habit_history",
    "Day-by-day completion history for one habit. Pass the habit name exactly as "
    "get_habits reports it. Optionally pass 'days' (default 14) for how far back "
    "to look. Use when asked about a pattern over time rather than the current "
    "number.",
    {"name": str},
    annotations=READ_ONLY,
)
async def get_habit_history(args: dict[str, Any]) -> dict[str, Any]:
    span = int(args.get("days", 14))
    name = args["name"].strip().lower()
    today = config.today()

    with _session() as s:
        habit = next(
            (
                h
                for h in s.exec(select(db.Habit)).all()
                if h.name.strip().lower() == name
            ),
            None,
        )
        if habit is None:
            known = [h.name for h in s.exec(select(db.Habit)).all()]
            return _error(
                f"No habit named {args['name']!r}. Known habits: "
                + (", ".join(known) if known else "none")
            )

        days = streaks.logged_days(s, habit.id)
        window = [today - timedelta(days=n) for n in range(span - 1, -1, -1)]
        done = [d for d in window if d in days]
        marks = "".join("#" if d in days else "." for d in window)

    return _text(
        f"{habit.name}, last {span} days (oldest first, # = done): {marks}\n"
        f"{len(done)}/{span} days completed."
    )


# --- tasks ------------------------------------------------------------------


@tool(
    "get_tasks",
    "The user's to-do list. Pass 'include_done' true to include completed tasks; "
    "by default only open ones are returned.",
    {},
    annotations=READ_ONLY,
)
async def get_tasks(args: dict[str, Any]) -> dict[str, Any]:
    include_done = bool(args.get("include_done", False))
    with _session() as s:
        stmt = select(db.Task).order_by(db.Task.done, db.Task.id.desc())
        if not include_done:
            stmt = stmt.where(db.Task.done == False)
        tasks = s.exec(stmt).all()

        if not tasks:
            return _text("No open tasks.")
        lines = [
            f"- [{'x' if t.done else ' '}] {t.title}"
            + (f" (due {t.due.isoformat()})" if t.due else "")
            for t in tasks
        ]
    return _text("Tasks:\n" + "\n".join(lines))


# --- career -----------------------------------------------------------------


@tool(
    "get_applications",
    "Job applications with their stage and next date. Optionally pass 'stage' to "
    "filter (applied, oa, phone, technical, final, offer, rejected).",
    {},
    annotations=READ_ONLY,
)
async def get_applications(args: dict[str, Any]) -> dict[str, Any]:
    stage = args.get("stage")
    with _session() as s:
        stmt = select(db.Application).order_by(db.Application.next_date)
        if stage:
            stmt = stmt.where(db.Application.stage == stage.strip().lower())
        apps = s.exec(stmt).all()

        if not apps:
            return _text(f"No applications{f' at stage {stage}' if stage else ''}.")
        lines = [
            f"- #{a.id} {a.company} - {a.role} [{a.stage}]"
            + (f", next {a.next_date.isoformat()}" if a.next_date else "")
            for a in apps
        ]
    return _text("Applications:\n" + "\n".join(lines))


@tool(
    "get_prep_notes",
    "Prep notes for one application, by its numeric id from get_applications. "
    "Returns unresolved notes by default; pass 'include_resolved' true for all.",
    {"application_id": int},
    annotations=READ_ONLY,
)
async def get_prep_notes(args: dict[str, Any]) -> dict[str, Any]:
    app_id = int(args["application_id"])
    include_resolved = bool(args.get("include_resolved", False))

    with _session() as s:
        application = s.get(db.Application, app_id)
        if application is None:
            return _error(f"No application with id {app_id}.")

        stmt = select(db.PrepNote).where(db.PrepNote.application_id == app_id)
        if not include_resolved:
            stmt = stmt.where(db.PrepNote.resolved == False)
        notes = s.exec(stmt.order_by(db.PrepNote.id)).all()

        if not notes:
            return _text(
                f"No {'' if include_resolved else 'unresolved '}prep notes for "
                f"{application.company} - {application.role}."
            )
        lines = [
            f"- [{n.kind}]{' (resolved)' if n.resolved else ''} {n.body}" for n in notes
        ]
        header = f"Prep notes for {application.company} - {application.role}:"
    return _text(header + "\n" + "\n".join(lines))


@tool(
    "get_skills",
    "Skills with current confidence against target, both on a 1-5 scale. Use to "
    "find where the gap between current and target is largest.",
    {},
    annotations=READ_ONLY,
)
async def get_skills(args: dict[str, Any]) -> dict[str, Any]:
    with _session() as s:
        skills = s.exec(select(db.Skill).order_by(db.Skill.name)).all()
        if not skills:
            return _text("No skills recorded yet.")
        lines = [
            f"- {sk.name}: {sk.confidence}/5, target {sk.target}"
            + (
                f" (gap {sk.target - sk.confidence})"
                if sk.target > sk.confidence
                else ""
            )
            + (
                f", last touched {sk.last_touched.isoformat()}"
                if sk.last_touched
                else ""
            )
            for sk in skills
        ]
    return _text("Skills:\n" + "\n".join(lines))


ALL_TOOLS = [
    get_habits,
    get_habit_history,
    get_tasks,
    get_applications,
    get_prep_notes,
    get_skills,
]

# Key in mcp_servers becomes the {server} in mcp__{server}__{tool}.
jarvis_server = create_sdk_mcp_server(name="jarvis", version="1.0.0", tools=ALL_TOOLS)

TOOL_NAMES = [f"mcp__jarvis__{t.name}" for t in ALL_TOOLS]
