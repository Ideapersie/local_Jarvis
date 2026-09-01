"""Agent tool handlers, exercised against a seeded in-memory database.

No API calls: these are plain async functions over SQLite, so they stay cheap
and run on every change. The end-to-end "did the agent actually call the tool"
check needs a live model and is a manual step, not a test.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session

from app import agent_tools, config
from app.agent import _is_writable
from app.db import Application, Habit, HabitLog, PrepNote, Skill, Task

TODAY = config.today()
HABIT_DAY = config.habit_day()  # not TODAY between midnight and 05:00


async def call_raw(_tool: str, **kwargs) -> dict:
    """Invoke a tool by name. Leading underscore so a tool argument called
    `name` (get_habit_history has one) cannot collide with this parameter."""
    handler = next(t for t in agent_tools.ALL_TOOLS if t.name == _tool)
    return await handler.handler(kwargs)


async def call(_tool: str, **kwargs) -> str:
    result = await call_raw(_tool, **kwargs)
    return result["content"][0]["text"]


@pytest.fixture()
def seeded(test_engine):
    """Data the tools read. test_engine redirects app.db.engine, so the tools -
    which resolve the engine at call time - hit this rather than jarvis.db."""
    with Session(test_engine) as s:
        s.add(Habit(id=1, name="leetcode", category="career", created_at=TODAY))
        s.add(
            Habit(
                id=2,
                name="creatine",
                category="health",
                reminder_time="18:00",
                created_at=TODAY,
            )
        )
        s.add(
            Habit(
                id=3,
                name="retired",
                category="personal",
                created_at=TODAY,
                active=False,
            )
        )
        # leetcode: a live 3-day streak ending today.
        for offset in (0, 1, 2):
            s.add(HabitLog(habit_id=1, day=HABIT_DAY - timedelta(days=offset)))

        s.add(Task(title="review backprop", created_at=datetime.now()))
        s.add(Task(title="done thing", done=True, created_at=datetime.now()))

        s.add(
            Application(
                id=1,
                company="Marshall Wace",
                role="Grad SWE",
                stage="technical",
                next_date=TODAY + timedelta(days=6),
                updated_at=datetime.now(),
            )
        )
        s.add(
            PrepNote(
                application_id=1,
                kind="topic_to_learn",
                body="backpressure in streaming systems",
                created_at=datetime.now(),
            )
        )
        s.add(
            PrepNote(
                application_id=1,
                kind="weakness",
                body="already handled",
                resolved=True,
                created_at=datetime.now(),
            )
        )
        s.add(Skill(name="kubernetes", confidence=2, target=4))
        s.commit()
        yield s


# --- habits -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_habits_reports_streaks_and_excludes_archived(seeded):
    out = await call("get_habits")
    assert "leetcode" in out and "creatine" in out
    assert "retired" not in out, "archived habits must not reach the agent"
    assert "current streak 3" in out
    assert "done today" in out


@pytest.mark.asyncio
async def test_get_habits_agrees_with_the_streaks_service(seeded):
    """The tool must not reimplement streak logic - it must reuse it."""
    from app.services import streaks

    expected = streaks.current_streak(seeded, 1, HABIT_DAY)
    out = await call("get_habits")
    assert f"current streak {expected}" in out


@pytest.mark.asyncio
async def test_get_habit_history_marks_completed_days(seeded):
    out = await call("get_habit_history", name="leetcode", days=5)
    assert out.rstrip().endswith("3/5 days completed.")
    assert "..###" in out  # oldest first, three most recent done


@pytest.mark.asyncio
async def test_unknown_habit_is_an_error_not_an_empty_answer(seeded):
    result = await call_raw("get_habit_history", name="nonexistent")
    assert result.get("is_error") is True
    # Naming the real habits lets the agent retry instead of guessing again.
    assert "leetcode" in result["content"][0]["text"]


# --- tasks ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tasks_hides_done_by_default(seeded):
    out = await call("get_tasks")
    assert "review backprop" in out
    assert "done thing" not in out


@pytest.mark.asyncio
async def test_get_tasks_can_include_done(seeded):
    out = await call("get_tasks", include_done=True)
    assert "done thing" in out


# --- career -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_applications_lists_stage_and_next_date(seeded):
    out = await call("get_applications")
    assert "Marshall Wace" in out and "technical" in out


@pytest.mark.asyncio
async def test_get_applications_filters_by_stage(seeded):
    assert "Marshall Wace" not in await call("get_applications", stage="offer")


@pytest.mark.asyncio
async def test_get_prep_notes_hides_resolved_by_default(seeded):
    out = await call("get_prep_notes", application_id=1)
    assert "backpressure" in out
    assert "already handled" not in out


@pytest.mark.asyncio
async def test_get_prep_notes_for_missing_application_is_an_error(seeded):
    result = await call_raw("get_prep_notes", application_id=999)
    assert result.get("is_error") is True


@pytest.mark.asyncio
async def test_get_skills_surfaces_the_gap(seeded):
    out = await call("get_skills")
    assert "kubernetes" in out and "gap 2" in out


# --- empty database ---------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_say_so_when_there_is_nothing(test_engine):
    """Empty must read as empty, never as an error or a fabricated row."""
    assert "No active habits" in await call("get_habits")
    assert "No open tasks" in await call("get_tasks")
    assert "No applications" in await call("get_applications")
    assert "No skills" in await call("get_skills")


# --- permission gate --------------------------------------------------------


@pytest.mark.parametrize(
    "path,allowed",
    [
        ("brain/profile.md", True),
        ("briefs/2026-08-10.md", True),
        ("brain/nested/deep.md", True),
        ("app/main.py", False),
        (".env", False),
        ("brain/../app/main.py", False),
        ("../escape.md", False),
    ],
)
def test_write_confinement(path, allowed):
    assert _is_writable(path) is allowed


def test_tool_names_are_namespaced():
    assert "mcp__jarvis__get_habits" in agent_tools.TOOL_NAMES
    assert len(agent_tools.TOOL_NAMES) == len(agent_tools.ALL_TOOLS)


# --- chat rendering ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("**bold**", "<strong>bold</strong>"),
        ("`code`", "<code>code</code>"),
        ("plain text", "plain text"),
        # Escaping must happen before tags are added, or model output could
        # inject elements into the page.
        ("<script>alert(1)</script>", "&lt;script&gt;alert(1)&lt;/script&gt;"),
        ("**<b>x</b>**", "<strong>&lt;b&gt;x&lt;/b&gt;</strong>"),
        ("5 > 3 & 2 < 4", "5 &gt; 3 &amp; 2 &lt; 4"),
    ],
)
def test_chat_render_escapes_then_formats(raw, expected):
    from app.routers.chat import _render

    assert _render(raw) == expected


def test_sse_frames_split_newlines():
    """A raw newline inside a payload would truncate the SSE frame."""
    from app.routers.chat import _sse

    frame = _sse("token", "line one\nline two")
    assert frame == "event: token\ndata: line one\ndata: line two\n\n"
