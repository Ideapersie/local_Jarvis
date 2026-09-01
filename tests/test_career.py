"""Career tracker: grouping, stage transitions, prep notes, skills, agent writes.

No model calls. The interview-prep skill's judgement is not testable offline,
but the routing and data handling around it are.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlmodel import Session, select

from app import agent_tools, config, db
from app.routers import career

TODAY = config.today()


async def call_raw(_tool: str, **kwargs) -> dict:
    handler = next(t for t in agent_tools.ALL_TOOLS if t.name == _tool)
    return await handler.handler(kwargs)


async def call(_tool: str, **kwargs) -> str:
    return (await call_raw(_tool, **kwargs))["content"][0]["text"]


def make_app(session, company="Marshall Wace", stage="technical", days=6, **kw):
    app = db.Application(
        company=company,
        role=kw.get("role", "Grad SWE"),
        stage=stage,
        next_date=TODAY + timedelta(days=days) if days is not None else None,
        updated_at=datetime.now(),
    )
    session.add(app)
    session.commit()
    session.refresh(app)
    return app


@pytest.fixture()
def store(test_engine):
    with Session(test_engine) as s:
        yield s


# --- grouping ---------------------------------------------------------------


def test_empty_stages_are_dropped(store):
    """An empty 'Offer' heading every day is discouraging and uninformative."""
    make_app(store, stage="applied")
    ctx = career.build_context(store)
    assert [g["stage"] for g in ctx["grouped"]] == ["applied"]


def test_groups_follow_pipeline_order(store):
    make_app(store, company="C", stage="final")
    make_app(store, company="A", stage="applied")
    make_app(store, company="B", stage="phone")
    stages = [g["stage"] for g in career.build_context(store)["grouped"]]
    assert stages == ["applied", "phone", "final"]


def test_rejected_sorts_last(store):
    make_app(store, company="Nope", stage="rejected", days=None)
    make_app(store, company="Live", stage="applied")
    stages = [g["stage"] for g in career.build_context(store)["grouped"]]
    assert stages[-1] == "rejected"


def test_live_count_excludes_rejected(store):
    make_app(store, company="A", stage="applied")
    make_app(store, company="B", stage="rejected", days=None)
    ctx = career.build_context(store)
    assert ctx["total"] == 2 and ctx["live"] == 1


# --- prep readiness ---------------------------------------------------------


@pytest.mark.parametrize(
    "days,ready",
    [
        (0, True),  # today
        (7, True),  # edge of the window
        (8, False),  # outside it
        (-1, False),  # already passed
        (None, False),  # no date at all
    ],
)
def test_prep_button_only_for_imminent_interviews(store, days, ready):
    app = make_app(store, days=days)
    assert career.build_application(store, app)["prep_ready"] is ready


def test_no_prep_for_rejected_or_offer(store):
    for stage in ("rejected", "offer"):
        app = make_app(store, company=stage, stage=stage, days=3)
        assert career.build_application(store, app)["prep_ready"] is False


def test_past_date_is_flagged_overdue(store):
    app = make_app(store, days=-2)
    assert career.build_application(store, app)["overdue"] is True


# --- stage transitions ------------------------------------------------------


def test_rejecting_clears_the_next_date(client, store):
    """A stale date on a rejected application would keep it in the brief's
    fourteen-day window forever."""
    app = make_app(store)
    client.post(
        f"/career/applications/{app.id}/stage",
        data={"stage": "rejected"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    store.expire_all()
    assert store.get(db.Application, app.id).next_date is None


def test_unknown_stage_is_rejected(client, store):
    app = make_app(store)
    r = client.post(
        f"/career/applications/{app.id}/stage",
        data={"stage": "banana"},
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    assert r.status_code == 400


def test_deleting_an_application_removes_its_notes(client, store):
    """SQLite will not cascade, so orphaned notes would accumulate invisibly."""
    app = make_app(store)
    store.add(
        db.PrepNote(
            application_id=app.id,
            kind="topic_to_learn",
            body="backpressure",
            created_at=datetime.now(),
        )
    )
    store.commit()

    client.request(
        "DELETE",
        f"/career/applications/{app.id}",
        headers={"Sec-Fetch-Site": "same-origin"},
    )
    store.expire_all()
    assert store.exec(select(db.PrepNote)).all() == []


# --- agent writes -----------------------------------------------------------


@pytest.mark.asyncio
async def test_agent_can_move_a_stage(store):
    app = make_app(store, stage="phone")
    out = await call("set_application_stage", application_id=app.id, stage="final")
    store.expire_all()
    assert store.get(db.Application, app.id).stage == "final"
    assert "phone" in out and "final" in out


@pytest.mark.asyncio
async def test_agent_rejecting_also_clears_the_date(store):
    app = make_app(store)
    await call("set_application_stage", application_id=app.id, stage="rejected")
    store.expire_all()
    assert store.get(db.Application, app.id).next_date is None


@pytest.mark.asyncio
async def test_agent_gets_an_error_for_a_bad_stage(store):
    """Naming the valid stages lets it retry instead of guessing again."""
    app = make_app(store)
    result = await call_raw(
        "set_application_stage", application_id=app.id, stage="nonsense"
    )
    assert result.get("is_error") is True
    assert "applied" in result["content"][0]["text"]


@pytest.mark.asyncio
async def test_agent_write_to_missing_application_errors(store):
    result = await call_raw("set_application_stage", application_id=999, stage="final")
    assert result.get("is_error") is True


@pytest.mark.asyncio
async def test_agent_can_add_a_prep_note(store):
    app = make_app(store)
    await call(
        "add_prep_note",
        application_id=app.id,
        kind="weakness",
        body="froze on backpressure",
    )
    store.expire_all()
    note = store.exec(select(db.PrepNote)).one()
    assert note.kind == "weakness" and "backpressure" in note.body


@pytest.mark.asyncio
async def test_agent_prep_note_rejects_unknown_kind(store):
    app = make_app(store)
    result = await call_raw(
        "add_prep_note", application_id=app.id, kind="vibes", body="x"
    )
    assert result.get("is_error") is True


@pytest.mark.asyncio
async def test_agent_can_set_a_date_and_clear_it(store):
    app = make_app(store, days=None)
    await call("set_application_date", application_id=app.id, date="2026-09-01")
    store.expire_all()
    assert store.get(db.Application, app.id).next_date.isoformat() == "2026-09-01"

    await call("set_application_date", application_id=app.id, date="")
    store.expire_all()
    assert store.get(db.Application, app.id).next_date is None


@pytest.mark.asyncio
async def test_agent_rejects_an_unparseable_date(store):
    app = make_app(store)
    result = await call_raw(
        "set_application_date", application_id=app.id, date="next tuesday"
    )
    assert result.get("is_error") is True


@pytest.mark.asyncio
async def test_skill_confidence_creates_then_updates(store):
    await call("set_skill_confidence", name="kubernetes", confidence=2, target=4)
    store.expire_all()
    skill = store.exec(select(db.Skill)).one()
    assert (skill.confidence, skill.target) == (2, 4)

    await call("set_skill_confidence", name="Kubernetes", confidence=4)
    store.expire_all()
    # Case-insensitive match, so it must update rather than create a duplicate.
    assert len(store.exec(select(db.Skill)).all()) == 1
    assert store.exec(select(db.Skill)).one().confidence == 4


@pytest.mark.asyncio
async def test_skill_confidence_is_clamped_to_the_scale(store):
    await call("set_skill_confidence", name="python", confidence=99)
    store.expire_all()
    assert store.exec(select(db.Skill)).one().confidence == 5


@pytest.mark.asyncio
async def test_habits_and_tasks_have_no_write_tools(store):
    """Deliberate: a fabricated habit log silently corrupts a streak."""
    names = {t.name for t in agent_tools.ALL_TOOLS}
    for forbidden in ("log_habit", "add_task", "set_habit", "complete_task"):
        assert forbidden not in names


def test_write_tools_are_registered():
    names = {t.name for t in agent_tools.CAREER_WRITE_TOOLS}
    assert names <= {t.name for t in agent_tools.ALL_TOOLS}
    assert "set_application_stage" in agent_tools.TOOL_NAMES


def test_career_writes_reach_the_model_through_a_profile():
    """The handlers are useless if no profile actually offers them."""
    from app import loop

    offered = {s["function"]["name"] for s in loop.specs_for("chat_write")}
    assert {t.name for t in agent_tools.CAREER_WRITE_TOOLS} <= offered
