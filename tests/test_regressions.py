"""One test per bug found in the Day 1 audit.

Each of these failed before its fix. They exist so the fix cannot silently
regress - especially the timezone one, whose symptom appears far from its cause.
"""

from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import event
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import config
from app.db import Habit, Task
from app.routers import habits as habits_router

SEED = date(2026, 1, 1)


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(eng)
    return eng


@pytest.fixture()
def session(engine):
    with Session(engine) as s:
        yield s


# --- Bug 1: timezone round-trip --------------------------------------------


def test_stored_datetime_is_comparable_after_read_back(session):
    """config.now() must survive SQLite, which has no timezone type.

    A tz-aware value is written and read back naive, so any later comparison
    raises `can't subtract offset-naive and offset-aware datetimes`.
    """
    from sqlmodel import select

    session.add(Task(title="x", created_at=config.now()))
    session.commit()

    stored = session.exec(select(Task)).first().created_at
    delta = config.now() - stored  # raised TypeError before the fix
    assert delta.total_seconds() >= 0


def test_now_is_naive():
    assert config.now().tzinfo is None


# --- Bug 2: N+1 queries -----------------------------------------------------


def test_panel_render_does_not_scale_queries_with_habit_count(engine):
    """build_row must fetch each habit's logs once, not once per derived number."""
    counter = {"n": 0}

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if statement.strip().upper().startswith("SELECT"):
            counter["n"] += 1

    with Session(engine) as s:
        for i in range(5):
            s.add(Habit(name=f"h{i}", category="health", created_at=SEED))
        s.commit()

        counter["n"] = 0
        rows = [habits_router.build_row(s, h) for h in habits_router.active_habits(s)]

    assert len(rows) == 5
    # One SELECT for the habit list plus one per habit. Was 21.
    assert counter["n"] < 10, f"{counter['n']} queries for 5 habits"


# --- Bug 3: archived habits accepting writes --------------------------------


def test_toggling_an_archived_habit_is_rejected(client):
    archive = client.post("/habits/1/archive")
    assert archive.status_code == 200

    blocked = client.post("/habits/1/toggle")
    assert blocked.status_code == 409


def test_archive_is_idempotent_and_keeps_the_original_date(client, app_session):
    from sqlmodel import select

    client.post("/habits/2/archive")
    first = app_session.exec(select(Habit).where(Habit.id == 2)).one().archived_at

    client.post("/habits/2/archive")
    app_session.expire_all()
    second = app_session.exec(select(Habit).where(Habit.id == 2)).one().archived_at

    assert first == second


# --- Bug 4: cross-origin and Host ------------------------------------------


def test_cross_site_post_is_blocked(client):
    r = client.post("/habits/3/toggle", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 403


def test_same_origin_post_still_works(client):
    r = client.post("/habits/3/toggle", headers={"Sec-Fetch-Site": "same-origin"})
    assert r.status_code == 200


def test_get_is_not_blocked_cross_site(client):
    """Safe methods stay open - blocking them would break ordinary navigation."""
    r = client.get("/", headers={"Sec-Fetch-Site": "cross-site"})
    assert r.status_code == 200


def test_unknown_host_is_rejected(client):
    r = client.get("/", headers={"Host": "attacker.example"})
    assert r.status_code == 400
