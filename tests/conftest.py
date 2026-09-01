"""Test fixtures.

Every fixture here binds the app to a throwaway in-memory database. Tests must
never touch the real jarvis.db - a test that archives a habit against live data
is a data-loss bug wearing a test's clothes.
"""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from app import db as db_module
from app import main as main_module
from app.db import Habit
from app.deps import get_session

SEED = date(2026, 1, 1)


@pytest.fixture(autouse=True)
def _never_the_real_database(monkeypatch):
    """Point app.db.engine at a throwaway database for EVERY test.

    The rule above was opt-in: a test had to remember to ask for test_engine.
    tests/test_portfolio.py built its own engine and did not, so every call to
    services.costs.record - which opens Session(db.engine) directly, whatever
    session the caller is holding - wrote a fabricated Opus charge into the real
    jarvis.db. Six of them, at a cost that never happened, which the dashboard
    would have shown as real spend.

    Autouse closes that by default. A test wanting the app wiring still asks for
    test_engine, which overrides this with its own engine.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(main_module, "engine", engine)
    return engine


@pytest.fixture()
def test_engine(monkeypatch):
    """In-memory engine, swapped in everywhere the real one is referenced.

    main.py does `from app.db import engine`, which binds the name at import
    time, so patching app.db alone would leave the lifespan pointed at the real
    database.
    """
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(main_module, "engine", engine)
    return engine


@pytest.fixture()
def app_session(test_engine):
    with Session(test_engine) as s:
        yield s


@pytest.fixture()
def client(test_engine, app_session):
    """TestClient with three seeded habits and the DB dependency redirected."""
    for i in range(1, 4):
        app_session.add(
            Habit(id=i, name=f"habit-{i}", category="health", created_at=SEED)
        )
    app_session.commit()

    def override():
        with Session(test_engine) as s:
            yield s

    main_module.app.dependency_overrides[get_session] = override
    with TestClient(main_module.app) as c:
        yield c
    main_module.app.dependency_overrides.clear()
