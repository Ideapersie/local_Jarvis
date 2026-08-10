"""Cost tracking and auth selection.

No API calls: record() and month_to_date() are plain database work, and
active_auth() is pure branching.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlmodel import Session

from app import agent, config, db
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


# --- auth selection ---------------------------------------------------------


def test_subscription_is_the_default(monkeypatch):
    monkeypatch.setattr(config, "AGENT_AUTH", "subscription")
    monkeypatch.setattr(agent, "_credit_exhausted", False)
    assert agent.active_auth() == "subscription"


def test_exhausted_credit_falls_back_to_the_key(monkeypatch):
    monkeypatch.setattr(config, "AGENT_AUTH", "subscription")
    monkeypatch.setattr(config, "AGENT_AUTH_FALLBACK", True)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(agent, "_credit_exhausted", True)
    assert agent.active_auth() == "api_key"


def test_no_fallback_without_a_key(monkeypatch):
    """With nothing to fall back to, stay put rather than claim a key exists."""
    monkeypatch.setattr(config, "AGENT_AUTH", "subscription")
    monkeypatch.setattr(config, "AGENT_AUTH_FALLBACK", True)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", None)
    monkeypatch.setattr(agent, "_credit_exhausted", True)
    assert agent.active_auth() == "subscription"


def test_fallback_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "AGENT_AUTH", "subscription")
    monkeypatch.setattr(config, "AGENT_AUTH_FALLBACK", False)
    monkeypatch.setattr(config, "ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setattr(agent, "_credit_exhausted", True)
    assert agent.active_auth() == "subscription"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("You have exceeded your credit for this month", True),
        ("rate limit reached", True),
        ("authentication failed", True),
        ("Tool execution failed: file not found", False),
        ("", False),
    ],
)
def test_credit_exhaustion_detection(text, expected):
    assert agent.looks_like_credit_exhaustion(text) is expected


def test_subscription_env_strips_the_api_key(monkeypatch):
    """The SDK prefers the key over the plan login, so it must not reach the
    subprocess - otherwise 'subscription' silently bills the API instead."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-pass")
    assert "ANTHROPIC_API_KEY" not in agent._subprocess_env()
