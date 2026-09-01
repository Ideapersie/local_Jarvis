"""Cost tracking and auth selection.

No API calls: record() and month_to_date() are plain database work, and
active_auth() is pure branching.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import Session

from app import config, db
from app.services import costs


@pytest.fixture()
def store(test_engine):
    with Session(test_engine) as s:
        yield s


def add(session, cost, tier="agent", when=None):
    session.add(
        db.CostLog(
            at=when or config.now(),
            kind="chat_agent",
            tier=tier,
            model="claude-sonnet-5",
            auth="subscription",
            cost_usd=cost,
        )
    )
    session.commit()


# --- month to date ----------------------------------------------------------


def test_month_to_date_sums_this_month_only(store):
    add(store, 0.20)
    add(store, 0.30)
    # Last month must not leak into this month's figure.
    first = config.now().replace(day=1)
    add(store, 99.0, when=first - timedelta(days=2))

    out = costs.month_to_date(store)
    assert out["total"] == pytest.approx(0.50)
    assert out["calls"] == 2


def test_month_to_date_splits_by_tier(store):
    add(store, 0.18, tier="agent")
    add(store, 0.0007, tier="quick")

    out = costs.month_to_date(store)
    assert out["by_tier"]["agent"] == pytest.approx(0.18)
    assert out["by_tier"]["quick"] == pytest.approx(0.0007)


def test_credit_remaining_tracks_the_plan(store, monkeypatch):
    monkeypatch.setattr(config, "PLAN", "pro")
    add(store, 5.0)

    out = costs.month_to_date(store)
    assert out["credit"] == 20.0
    assert out["remaining"] == pytest.approx(15.0)
    assert out["pct"] == pytest.approx(25.0)


def test_overspend_clamps_rather_than_going_negative(store, monkeypatch):
    monkeypatch.setattr(config, "PLAN", "pro")
    add(store, 25.0)

    out = costs.month_to_date(store)
    assert out["remaining"] == 0.0
    assert out["pct"] == 100.0  # bar must not overflow its track


def test_no_plan_reports_no_ceiling(store, monkeypatch):
    monkeypatch.setattr(config, "PLAN", "none")
    add(store, 5.0)

    out = costs.month_to_date(store)
    assert out["remaining"] is None and out["pct"] is None


def test_empty_month_is_zero_not_an_error(store):
    out = costs.month_to_date(store)
    assert out["total"] == 0.0 and out["calls"] == 0


def test_today_total_excludes_yesterday(store):
    add(store, 1.0)
    add(store, 5.0, when=config.now() - timedelta(days=1))
    assert costs.today_total(store) == pytest.approx(1.0)


def test_record_never_raises_on_a_broken_engine(monkeypatch):
    """A failed cost write must not take down the turn it was measuring."""
    monkeypatch.setattr(db, "engine", None)
    costs.record("chat_agent", "agent", "m", 0.1)  # must not raise


def test_record_tolerates_a_null_cost(test_engine, store):
    costs.record("chat_agent", "agent", "m", None)
    assert costs.month_to_date(store)["calls"] == 1


# The auth-selection tests lived here. They covered app/agent.py's subscription
# credit and its fallback to an API key when that credit ran out - machinery
# that only existed because claude-agent-sdk spawned a CLI subprocess with its
# own login. Every tier except portfolio now runs locally for nothing, so there
# is no credit to exhaust and nothing to fall back to. The tests are removed
# rather than skipped: a skipped test for deleted behaviour reads as a gap.
