"""SQLModel models, engine, create_all, and first-run seed.

Structured state only. Narrative state lives in brain/*.md.
Streaks are deliberately absent: they are computed from HabitLog, never stored.
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, Session, SQLModel, create_engine, select

from app import config


class Habit(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    category: str  # health | career | personal
    reminder_time: str | None = None  # "18:00", None for anytime
    active: bool = True
    created_at: date
    archived_at: date | None = None  # soft delete, keeps history intact


class HabitLog(SQLModel, table=True):
    __table_args__ = (
        UniqueConstraint("habit_id", "day", name="uq_habitlog_habit_day"),
    )

    id: int | None = Field(default=None, primary_key=True)
    habit_id: int = Field(foreign_key="habit.id", index=True)
    day: date = Field(index=True)
    done: bool = True


class Task(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    title: str
    done: bool = False
    due: date | None = None
    created_at: datetime


class Application(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    company: str
    role: str
    stage: str  # applied | oa | phone | technical | final | offer | rejected
    next_date: date | None = None
    source: str | None = None
    notion_id: str | None = None  # for later two-way sync
    updated_at: datetime


class PrepNote(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    application_id: int = Field(foreign_key="application.id", index=True)
    kind: str  # topic_to_learn | weakness | question_asked | outcome
    body: str
    resolved: bool = False
    created_at: datetime


class Skill(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    name: str
    confidence: int  # 1-5, you set it, agent suggests changes
    target: int  # where you want it
    last_touched: date | None = None


class Reminder(SQLModel, table=True):
    """A nudge the app decided to send.

    Persisted even when Telegram delivers it, so the dashboard can show what was
    sent and the 18:00 check is verifiable after the fact rather than only in a
    log line.
    """

    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(index=True)
    kind: str  # creatine | brief | reflection | manual
    body: str
    delivered_via: str  # telegram | log
    seen: bool = False


class Brief(SQLModel, table=True):
    """Index of written briefs. The prose lives in briefs/YYYY-MM-DD.md."""

    id: int | None = Field(default=None, primary_key=True)
    day: date = Field(index=True, unique=True)
    summary: str  # short version the dashboard panel renders
    path: str
    urgent: str | None = None  # newline-separated, drives the Urgent card
    created_at: datetime


class CostLog(SQLModel, table=True):
    """One row per model call, so spend is measured rather than guessed.

    total_cost_usd comes back on every agent turn and is otherwise discarded.
    Recording it is what makes "is this too expensive" answerable.
    """

    id: int | None = Field(default=None, primary_key=True)
    at: datetime = Field(index=True)
    kind: str  # chat_quick | chat_agent | brief | reflection | portfolio | triage
    tier: str  # quick | agent
    model: str
    auth: str  # subscription | api_key - which pool paid for it
    cost_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


class Holding(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    ticker: str = Field(index=True)
    quantity: float
    avg_price: float
    horizon: str  # long | short
    synced_at: datetime


engine = create_engine(
    config.DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False},
)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)


SEED_HABITS: list[tuple[str, str, str | None]] = [
    ("workout", "health", "20:00"),
    ("creatine", "health", "18:00"),
    ("call home", "personal", None),
    ("leetcode", "career", None),
    ("ai research", "career", None),
]


def seed_habits(session: Session) -> int:
    """Insert the five starting habits, but only into an empty table."""
    if session.exec(select(Habit)).first() is not None:
        return 0
    created = config.today()
    for name, category, reminder in SEED_HABITS:
        session.add(
            Habit(
                name=name,
                category=category,
                reminder_time=reminder,
                created_at=created,
            )
        )
    session.commit()
    return len(SEED_HABITS)
