"""Portfolio: the panel maths, and the boundary where money gets spent.

No network, and no Anthropic call is ever made - the remote provider is faked.
The tests that matter most here are the ones about cost and about the advice
guardrail, because both fail silently in production.
"""

from __future__ import annotations

import pytest
from sqlmodel import Session, SQLModel, create_engine

from app import config, db
from app.integrations import trading212
from app.llm.base import Completion, Usage
from app.services import portfolio


@pytest.fixture
def store():
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    with Session(engine) as s:
        yield s


def _pos(ticker="AAPL", qty=10.0, avg=100.0, now=110.0):
    return trading212.Position(
        ticker=ticker, quantity=qty, avg_price=avg, current_price=now
    )


def _account(positions=None, cash=500.0):
    positions = positions if positions is not None else [_pos()]
    return trading212.Account(
        positions=positions,
        cash_free=cash,
        total_value=cash + sum(p.value for p in positions),
        day_change_pct=1.5,
    )


# --- position maths ---------------------------------------------------------


def test_value_and_pnl():
    p = _pos(qty=10, avg=100, now=110)
    assert p.value == pytest.approx(1100)
    assert p.cost == pytest.approx(1000)
    assert p.pnl_pct == pytest.approx(10.0)


def test_zero_cost_basis_does_not_divide_by_zero():
    """A free share from a referral has avg_price 0 and must not crash the panel."""
    assert _pos(avg=0, now=50).pnl_pct == 0.0


# --- donut ------------------------------------------------------------------


def test_segments_sum_to_one_hundred():
    segs = portfolio._segments([_pos("A", 10, 10, 10), _pos("B", 10, 10, 30)])
    assert sum(s["pct"] for s in segs) == pytest.approx(100.0)


def test_segments_are_largest_first():
    segs = portfolio._segments([_pos("small", 1, 1, 1), _pos("big", 100, 1, 1)])
    assert segs[0]["ticker"] == "big"


def test_no_positions_means_no_segments():
    assert portfolio._segments([]) == []


def test_every_segment_gets_a_colour():
    segs = portfolio._segments([_pos(str(i), 1, 1, 1) for i in range(10)])
    assert all(s["colour"].startswith("#") for s in segs)


# --- the panel shows nothing rather than zeros ------------------------------


def test_view_is_none_before_the_first_sync(store):
    """app/main.py's own comment: placeholder numbers get read as real ones."""
    assert portfolio.view(store) is None


def test_view_never_touches_the_network(store, monkeypatch):
    """It runs on every dashboard render; the API is rate limited."""

    def _boom():
        raise AssertionError("view() must not call Trading212")

    monkeypatch.setattr(trading212, "fetch", _boom)
    portfolio.view(store)


def test_view_matches_the_template_contract(store, monkeypatch):
    monkeypatch.setattr(trading212, "fetch", lambda: _account())
    portfolio.sync(store)
    v = portfolio.view(store)
    # These keys are read directly by templates/partials/portfolio_panel.html.
    for key in ("segments", "total_value", "day_change", "cash_free", "market_note"):
        assert key in v
    assert isinstance(v["day_change"], float)


# --- spending money ---------------------------------------------------------


class _FakeRemote:
    def __init__(self, text="Concentration is high.", cost=0.0123):
        self.text = text
        self.cost = cost
        self.calls = 0
        self.system = None

    def complete(self, **kwargs):
        self.calls += 1
        self.system = kwargs.get("system")
        self.model = kwargs.get("model")
        return Completion(
            text=self.text,
            usage=Usage(1000, 200),
            cost_usd=self.cost,
            model=kwargs.get("model", "claude-opus-5"),
        )


def test_the_panel_never_calls_the_paid_model(store, monkeypatch):
    """A panel that billed Opus on every refresh would be the costliest bug here."""
    fake = _FakeRemote()
    monkeypatch.setattr(portfolio.remote, "provider", fake)
    monkeypatch.setattr(trading212, "fetch", lambda: _account())
    portfolio.sync(store)
    portfolio.view(store)
    portfolio.view(store)
    assert fake.calls == 0


def test_analyse_uses_opus_and_records_the_cost(store, monkeypatch):
    fake = _FakeRemote()
    recorded = {}
    monkeypatch.setattr(portfolio.remote, "provider", fake)
    monkeypatch.setattr(trading212, "fetch", lambda: _account())
    monkeypatch.setattr(
        portfolio.costs,
        "record",
        lambda kind, tier, model, cost, auth="", usage=None: recorded.update(
            {"kind": kind, "model": model, "cost": cost}
        ),
    )

    note = portfolio.analyse(store)
    assert note is not None
    assert fake.calls == 1
    assert fake.model == config.MODEL_PORTFOLIO == "claude-opus-5"
    assert recorded["kind"] == "portfolio"
    assert recorded["cost"] == pytest.approx(0.0123)


def test_analyse_forbids_advice_in_the_prompt(store, monkeypatch):
    """.claude/CLAUDE.md: present reasoning, never a recommendation.

    Enforced in the system prompt rather than hoped for, so this asserts on the
    instruction actually sent.
    """
    fake = _FakeRemote()
    monkeypatch.setattr(portfolio.remote, "provider", fake)
    monkeypatch.setattr(trading212, "fetch", lambda: _account())
    portfolio.analyse(store)

    lowered = fake.system.lower()
    assert "must not tell the user what to buy, sell, or hold" in lowered
    assert "the decision is theirs" in lowered


def test_analyse_is_skipped_with_nothing_to_analyse(store, monkeypatch):
    fake = _FakeRemote()
    monkeypatch.setattr(portfolio.remote, "provider", fake)
    monkeypatch.setattr(trading212, "fetch", lambda: None)
    assert portfolio.analyse(store) is None
    assert fake.calls == 0


def test_the_note_is_stored_so_it_is_not_re_billed(store, monkeypatch):
    fake = _FakeRemote(text="Two positions, both tech.")
    monkeypatch.setattr(portfolio.remote, "provider", fake)
    monkeypatch.setattr(trading212, "fetch", lambda: _account())
    monkeypatch.setattr(portfolio.costs, "record", lambda *a, **k: None)

    portfolio.sync(store)
    portfolio.analyse(store)
    assert portfolio.latest_note(store).body == "Two positions, both tech."

    v = portfolio.view(store)
    assert v["market_note"]["text"] == "Two positions, both tech."
    assert fake.calls == 1


# --- sync -------------------------------------------------------------------


def test_sync_replaces_rather_than_accumulates(store, monkeypatch):
    """Re-syncing must not leave a sold position on the books."""
    monkeypatch.setattr(trading212, "fetch", lambda: _account([_pos("A"), _pos("B")]))
    assert portfolio.sync(store) == 2
    monkeypatch.setattr(trading212, "fetch", lambda: _account([_pos("A")]))
    assert portfolio.sync(store) == 1

    from sqlmodel import select

    tickers = [h.ticker for h in store.exec(select(db.Holding)).all()]
    assert tickers == ["A"]


def test_sync_reports_failure_rather_than_wiping_holdings(store, monkeypatch):
    monkeypatch.setattr(trading212, "fetch", lambda: _account([_pos("A")]))
    portfolio.sync(store)
    monkeypatch.setattr(trading212, "fetch", lambda: None)
    assert portfolio.sync(store) == -1

    from sqlmodel import select

    assert len(store.exec(select(db.Holding)).all()) == 1


# --- the integration --------------------------------------------------------


def test_no_key_means_unavailable(monkeypatch):
    monkeypatch.setattr(config, "TRADING212_API_KEY", None)
    assert trading212.available() is False
    assert trading212.fetch() is None


def test_default_base_url_is_the_demo_account():
    """Pointing at the live account should be a deliberate edit, not a default."""
    assert "demo" in config.TRADING212_BASE_URL
