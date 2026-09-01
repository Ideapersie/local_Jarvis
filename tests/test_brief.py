"""Brief assembly, the creatine boolean, and weather. No model calls.

The agent's judgement is not testable offline, but everything feeding it is -
and that is the half where a wrong number does real damage.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlmodel import Session, select

from app import config, db, jobs
from app.integrations import gcal, notify, weather
from app.services import brief_builder, triage

TODAY = config.today()
HABIT_DAY = config.habit_day()  # not TODAY between midnight and 05:00


@pytest.fixture()
def seeded(test_engine):
    with Session(test_engine) as s:
        s.add(
            db.Habit(
                id=1,
                name="creatine",
                category="health",
                reminder_time="18:00",
                created_at=TODAY,
            )
        )
        s.add(db.Habit(id=2, name="leetcode", category="career", created_at=TODAY))
        for off in (0, 1, 2):
            s.add(db.HabitLog(habit_id=2, day=TODAY - timedelta(days=off)))
        s.add(db.Task(title="review backprop", created_at=datetime.now()))
        s.add(
            db.Application(
                id=1,
                company="Marshall Wace",
                role="Grad SWE",
                stage="technical",
                next_date=TODAY + timedelta(days=6),
                updated_at=datetime.now(),
            )
        )
        s.add(
            db.Application(
                id=2,
                company="Distant Co",
                role="SWE",
                stage="applied",
                next_date=TODAY + timedelta(days=90),
                updated_at=datetime.now(),
            )
        )
        s.commit()
        yield s


# --- gathering --------------------------------------------------------------


def test_gather_includes_streaks_and_open_tasks(seeded, monkeypatch):
    monkeypatch.setattr(weather, "fetch", lambda **kw: None)
    facts = brief_builder.gather(TODAY)

    leetcode = next(h for h in facts["habits"] if h["name"] == "leetcode")
    assert leetcode["current"] == 3
    assert [t["title"] for t in facts["tasks"]] == ["review backprop"]


def test_gather_only_looks_ahead_two_weeks(seeded, monkeypatch):
    """A screening call in 90 days is not news at 06:30."""
    monkeypatch.setattr(weather, "fetch", lambda **kw: None)
    companies = [a["company"] for a in brief_builder.gather(TODAY)["applications"]]
    assert companies == ["Marshall Wace"]


def test_past_interviews_are_not_upcoming(seeded, monkeypatch, test_engine):
    """A date that has already passed must not render as "in -6 days".

    The window was bounded at the top only, so a stale application sat under
    the "UPCOMING (next 14 days)" heading indefinitely, contradicting it.
    """
    monkeypatch.setattr(weather, "fetch", lambda **kw: None)
    monkeypatch.setattr(gcal, "available", lambda: False)
    with Session(test_engine) as s:
        s.add(
            db.Application(
                company="Stale Corp",
                role="Intern",
                stage="technical",
                next_date=TODAY - timedelta(days=6),
                updated_at=datetime.combine(TODAY, datetime.min.time()),
            )
        )
        s.commit()

    rendered = brief_builder.render_facts(brief_builder.gather(TODAY))
    assert "Stale Corp" not in rendered
    assert "in -" not in rendered


def test_missed_yesterday_is_flagged(seeded, monkeypatch):
    monkeypatch.setattr(weather, "fetch", lambda **kw: None)
    facts = brief_builder.gather(TODAY)
    creatine = next(h for h in facts["habits"] if h["name"] == "creatine")
    assert creatine["done_yesterday"] is False


def test_facts_forbid_inventing_absent_sources(seeded, monkeypatch):
    """Missing integrations must read as absent, not as empty.

    "No meetings today" when no calendar exists is a fabricated observation.
    """
    monkeypatch.setattr(weather, "fetch", lambda **kw: None)
    # Stubbed rather than left to the machine: this assertion used to depend on
    # whether the developer happened to have credentials.json on disk, so it
    # passed on a clean checkout and failed on a configured one.
    monkeypatch.setattr(gcal, "available", lambda: False)
    rendered = brief_builder.render_facts(brief_builder.gather(TODAY))
    assert "do not mention weather" in rendered.lower()
    assert "CALENDAR: not connected" in rendered
    assert "checked and is clear" not in rendered
    assert "do not invent interviews" not in rendered  # has a real application


def test_a_failed_calendar_fetch_is_not_an_empty_calendar(test_engine, monkeypatch):
    """Credentials on disk, but the call did not come back.

    The gap this closes: gcal.available() only checks that credentials.json
    exists, so a revoked token or an uninstalled client library rendered as
    "the calendar was checked and is clear" - the exact fabrication the
    not-connected branch was written to prevent.
    """
    monkeypatch.setattr(gcal, "available", lambda: True)
    monkeypatch.setattr(gcal, "upcoming", lambda **kw: None)
    monkeypatch.setattr(triage, "available", lambda: False)
    monkeypatch.setattr(weather, "fetch", lambda **kw: None)

    rendered = brief_builder.render_facts(brief_builder.gather(TODAY))
    assert "CALENDAR: not connected" in rendered
    assert "checked and is clear" not in rendered


def test_facts_warn_against_inventing_interviews(test_engine, monkeypatch):
    monkeypatch.setattr(weather, "fetch", lambda **kw: None)
    rendered = brief_builder.render_facts(brief_builder.gather(TODAY))
    assert "Do not invent interviews" in rendered


# --- urgent extraction ------------------------------------------------------


def test_urgent_section_is_lifted_out():
    body = "# Brief\n\n## Today\nstuff\n\n## Urgent\n- Interview at 9pm\n- Deadline\n"
    assert brief_builder._extract_urgent(body) == "Interview at 9pm\nDeadline"


def test_no_urgent_section_yields_none():
    assert brief_builder._extract_urgent("# Brief\n\n## Today\nquiet day\n") is None


def test_urgent_stops_at_the_next_heading():
    body = "## Urgent\n- One\n\n## Watch\n- not urgent\n"
    assert brief_builder._extract_urgent(body) == "One"


# --- the 18:00 boolean ------------------------------------------------------


def test_creatine_due_when_not_logged(seeded):
    assert jobs.creatine_due() is True


def test_creatine_not_due_once_logged(seeded):
    seeded.add(db.HabitLog(habit_id=1, day=HABIT_DAY))
    seeded.commit()
    assert jobs.creatine_due() is False


def test_creatine_not_due_if_the_habit_is_archived(seeded):
    habit = seeded.get(db.Habit, 1)
    habit.active = False
    seeded.add(habit)
    seeded.commit()
    assert jobs.creatine_due() is False


def test_creatine_sends_exactly_one_reminder(seeded, test_engine):
    jobs.creatine_check()
    sent = seeded.exec(select(db.Reminder).where(db.Reminder.kind == "creatine")).all()
    assert len(sent) == 1


def test_creatine_sends_nothing_when_done(seeded, test_engine):
    seeded.add(db.HabitLog(habit_id=1, day=HABIT_DAY))
    seeded.commit()
    jobs.creatine_check()
    assert (
        seeded.exec(select(db.Reminder).where(db.Reminder.kind == "creatine")).all()
        == []
    )


# --- delivery ---------------------------------------------------------------


def test_falls_back_to_log_without_telegram(test_engine, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", None)
    assert notify.send("test", kind="manual") == "log"


def test_reminder_is_persisted_either_way(test_engine, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", None)
    notify.send("persisted", kind="manual")
    with Session(test_engine) as s:
        assert s.exec(select(db.Reminder)).one().body == "persisted"


# --- weather ----------------------------------------------------------------


def test_two_lookup_pattern_brackets_the_event():
    """Spec section 8: one hour before, two hours after - as plain numbers."""
    hours = {
        h: weather.HourPoint(hour=h, temp_c=20.0 + h, rain_pct=h, condition="clear")
        for h in range(24)
    }
    day = weather.DayWeather(location="london", day=date(2026, 8, 10), hours=hours)

    before, after = day.around(20)
    assert before.hour == 19 and after.hour == 22


def test_lookup_clamps_at_midnight():
    hours = {
        h: weather.HourPoint(hour=h, temp_c=20.0, rain_pct=0, condition="clear")
        for h in range(24)
    }
    day = weather.DayWeather(location="london", day=date(2026, 8, 10), hours=hours)
    assert day.around(23)[1].hour == 23  # would be 25 unclamped
    assert day.around(0)[0].hour == 0


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Football at Regents Park, London", "london"),
        ("dinner in Bangkok", "bangkok"),
        ("somewhere unknown", "bangkok"),  # falls back to configured default
        (None, "bangkok"),
    ],
)
def test_location_resolution(text, expected, monkeypatch):
    monkeypatch.setattr(config, "DEFAULT_LOCATION", "bangkok")
    assert weather.resolve_location(text) == expected


def test_unknown_weather_code_is_not_guessed():
    assert weather.describe(None) == "unknown"
    assert weather.describe(9999) == "unknown"
