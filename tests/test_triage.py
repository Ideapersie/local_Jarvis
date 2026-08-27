"""Triage, Gmail parsing, calendar handling, and how they reach the brief.

No network and no model calls: the Google clients are stubbed, so these test the
parsing and decision logic rather than the APIs.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import ClassVar

import pytest
from sqlmodel import Session, select

from app import config, db
from app.integrations import gcal, gmail
from app.services import brief_builder, triage

TODAY = config.today()


def message(mid="m1", sender="Ada <ada@example.com>", subject="Hello", **kw):
    name, email = gmail._split_sender(sender)
    return gmail.Message(
        id=mid,
        thread_id=kw.get("thread_id", mid),
        sender=name,
        sender_email=email,
        subject=subject,
        snippet=kw.get("snippet", ""),
        platform=gmail._platform_for(email),
    )


@pytest.fixture()
def store(test_engine):
    with Session(test_engine) as s:
        yield s


# --- sender parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw,name,email",
    [
        ("Ada Lovelace <ada@x.com>", "Ada Lovelace", "ada@x.com"),
        ('"Quoted Name" <q@x.com>', "Quoted Name", "q@x.com"),
        ("bare@x.com", "bare@x.com", "bare@x.com"),
        ("<only@x.com>", "only@x.com", "only@x.com"),
    ],
)
def test_sender_parsing(raw, name, email):
    assert gmail._split_sender(raw) == (name, email)


@pytest.mark.parametrize(
    "email,platform",
    [
        ("jobs-noreply@linkedin.com", "linkedin"),
        ("notify@whatsapp.com", "whatsapp"),
        ("someone@gmail.com", None),
        # Must match the domain, not the word appearing anywhere.
        ("linkedin.fan@example.com", None),
    ],
)
def test_platform_detection(email, platform):
    assert gmail._platform_for(email) == platform


# --- classification guards --------------------------------------------------


def test_hallucinated_ids_are_dropped(monkeypatch):
    """A category attached to a message that was never sent would be stored as
    fact about an email that does not exist."""

    class FakeBlock:
        type = "text"
        text = (
            '{"items": [{"id": "real", "category": "fyi"}, '
            '{"id": "ghost", "category": "reply_needed"}]}'
        )

    class FakeUsage:
        input_tokens = 10
        output_tokens = 5

    class FakeResp:
        content: ClassVar = [FakeBlock()]
        usage = FakeUsage()

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                return FakeResp()

    monkeypatch.setattr("app.quick.client", lambda: FakeClient())
    out = triage.classify([message(mid="real")])
    assert out == {"real": "fyi"}


def test_unknown_category_is_dropped(monkeypatch):
    class FakeBlock:
        type = "text"
        text = '{"items": [{"id": "real", "category": "spam_probably"}]}'

    class FakeUsage:
        input_tokens = 10
        output_tokens = 5

    class FakeResp:
        content: ClassVar = [FakeBlock()]
        usage = FakeUsage()

    class FakeClient:
        class messages:
            @staticmethod
            def create(**kw):
                return FakeResp()

    monkeypatch.setattr("app.quick.client", lambda: FakeClient())
    assert triage.classify([message(mid="real")]) == {}


def test_empty_input_makes_no_call():
    assert triage.classify([]) == {}


# --- persistence ------------------------------------------------------------


def add_row(session, mid="m1", category="reply_needed", handled=False):
    session.add(
        db.Triage(
            message_id=mid,
            thread_id=mid,
            sender="Ada",
            sender_email="ada@x.com",
            subject="Subject",
            category=category,
            handled=handled,
            seen_at=config.now(),
        )
    )
    session.commit()


def test_pending_only_returns_unhandled_reply_needed(store):
    add_row(store, "a", "reply_needed")
    add_row(store, "b", "reply_needed", handled=True)
    add_row(store, "c", "fyi")
    assert [t.message_id for t in triage.pending(store)] == ["a"]


def test_summary_counts_unhandled_by_category(store):
    add_row(store, "a", "reply_needed")
    add_row(store, "b", "fyi")
    add_row(store, "c", "fyi", handled=True)
    assert triage.summary(store) == {"reply_needed": 1, "fyi": 1}


def test_marking_handled_removes_it_from_pending(client, store):
    add_row(store, "a", "reply_needed")
    row = store.exec(select(db.Triage)).one()

    client.post(
        f"/brief/inbox/{row.id}/handled", headers={"Sec-Fetch-Site": "same-origin"}
    )
    store.expire_all()
    assert triage.pending(store) == []


# --- calendar ---------------------------------------------------------------


def test_all_day_events_have_no_hour():
    stamp, day, all_day = gcal._parse({"date": "2026-08-14"})
    assert stamp is None and all_day is True and day == date(2026, 8, 14)


def test_timed_events_are_naive_local():
    """Everything else in this app stores naive local time; a tz-aware event
    would reintroduce the comparison bug fixed on day one."""
    stamp, _day, all_day = gcal._parse({"dateTime": "2026-08-14T20:00:00+07:00"})
    assert all_day is False
    assert stamp.tzinfo is None and stamp.hour == 20


def test_unparseable_dates_are_skipped():
    assert gcal._parse({"dateTime": "not a date"})[1] is None
    assert gcal._parse({})[1] is None


@pytest.mark.parametrize(
    "summary,location,outdoors",
    [
        ("Football with the lads", None, True),
        ("Standup", None, False),
        ("Coffee", "Regents Park", True),
        ("Morning run", None, True),
    ],
)
def test_outdoor_detection(summary, location, outdoors):
    event = gcal.Event(
        summary=summary,
        start=datetime(2026, 8, 14, 18, 0),
        day=date(2026, 8, 14),
        all_day=False,
        location=location,
    )
    assert event.outdoors is outdoors


def test_all_day_events_are_never_outdoors():
    """No hour means nothing to bracket, whatever the title says."""
    event = gcal.Event(
        summary="Football tournament",
        start=None,
        day=date(2026, 8, 14),
        all_day=True,
    )
    assert event.outdoors is False


# --- what reaches the brief -------------------------------------------------


def _no_google(monkeypatch):
    monkeypatch.setattr(gcal, "available", lambda: False)
    monkeypatch.setattr(triage, "available", lambda: False)
    monkeypatch.setattr("app.integrations.weather.fetch", lambda **kw: None)


def test_absent_sources_are_stated_as_absent(test_engine, monkeypatch):
    """Not connected and connected-but-empty are different claims. Saying the
    calendar is clear when there is no calendar is a fabricated observation."""
    _no_google(monkeypatch)
    rendered = brief_builder.render_facts(brief_builder.gather(TODAY))
    assert "CALENDAR: not connected" in rendered
    assert "EMAIL: not connected" in rendered
    assert "would imply you checked" in rendered
    # And crucially, not the phrasing used when they are connected and empty.
    assert "checked and is clear" not in rendered


def test_connected_but_empty_calendar_says_so(test_engine, monkeypatch):
    monkeypatch.setattr(gcal, "available", lambda: True)
    monkeypatch.setattr(gcal, "upcoming", lambda **kw: [])
    monkeypatch.setattr(triage, "available", lambda: False)
    monkeypatch.setattr("app.integrations.weather.fetch", lambda **kw: None)

    rendered = brief_builder.render_facts(brief_builder.gather(TODAY))
    assert "checked and is clear" in rendered


def test_events_reach_the_brief(test_engine, monkeypatch):
    event = gcal.Event(
        summary="Marshall Wace interview",
        start=datetime.combine(TODAY, datetime.min.time()).replace(hour=14),
        day=TODAY,
        all_day=False,
        location=None,
    )
    monkeypatch.setattr(gcal, "available", lambda: True)
    monkeypatch.setattr(gcal, "upcoming", lambda **kw: [event])
    monkeypatch.setattr(triage, "available", lambda: False)
    monkeypatch.setattr("app.integrations.weather.fetch", lambda **kw: None)

    rendered = brief_builder.render_facts(brief_builder.gather(TODAY))
    assert "Marshall Wace interview" in rendered
    assert "14:00" in rendered


def test_reply_needed_reaches_the_brief(test_engine, monkeypatch, store):
    add_row(store, "a", "reply_needed")
    store.exec(select(db.Triage)).one().why = "Confirm availability."
    store.commit()

    monkeypatch.setattr(gcal, "available", lambda: False)
    monkeypatch.setattr(triage, "available", lambda: True)
    monkeypatch.setattr("app.integrations.weather.fetch", lambda **kw: None)

    rendered = brief_builder.render_facts(brief_builder.gather(TODAY))
    assert "NEEDS A REPLY" in rendered
    assert "Subject" in rendered
