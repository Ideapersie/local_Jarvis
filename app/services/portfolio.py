"""Portfolio panel, and the one analysis that costs money.

Two deliberately separate things:

  view()     builds the panel from Holding rows. Free, runs on every page load.
  analyse()  one Claude Opus call. Explicit, never automatic.

The split is the cost control. A panel that quietly billed Opus on every refresh
would be the single most expensive thing in this project; the analysis is
triggered by a button and cached in the database until the user asks again.

On the guardrail. .claude/CLAUDE.md says not to make financial recommendations
framed as advice - present reasoning and let the user decide. That is enforced
here in the prompt rather than hoped for: the model is told to describe what
changed and what it would want to know, and told explicitly not to say what to
buy, sell or hold. The panel field is called market_note and suggestions, not
'recommendation', for the same reason.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import Session, select

from app import config, db
from app.integrations import trading212
from app.llm import remote
from app.llm.base import LLMError, Message
from app.services import costs

log = logging.getLogger("jarvis.portfolio")

# Colours for the allocation donut, in order. Deliberately muted: this is a
# dashboard panel, not a pitch deck.
SEGMENT_COLOURS = [
    "#7ba3c9",
    "#a8b89a",
    "#c9a87b",
    "#b89aa8",
    "#9aa8b8",
    "#c9c07b",
    "#8fa8a0",
]

SYSTEM = """You are looking at one person's small personal portfolio.

Describe what the numbers show and what would be worth knowing before making a
decision. Point out concentration, an unusually large move, or a position that
has drifted far from its cost.

You must NOT tell the user what to buy, sell, or hold, and you must not phrase
anything as a recommendation. No "consider trimming", no "it may be wise to".
State what is true and what you would want to check. The decision is theirs.

Be specific and short. Two or three sentences. Quote the real numbers."""


def sync(session: Session) -> int:
    """Pull positions into the Holding table. Returns rows written, -1 on failure."""
    account = trading212.fetch()
    if account is None:
        return -1

    now = config.now()
    for existing in session.exec(select(db.Holding)).all():
        session.delete(existing)

    for p in account.positions:
        session.add(
            db.Holding(
                ticker=p.ticker,
                quantity=p.quantity,
                avg_price=p.avg_price,
                current_price=p.current_price,
                horizon="long",
                synced_at=now,
            )
        )
    session.commit()
    log.info("synced %d holdings", len(account.positions))
    return len(account.positions)


def _segments(positions: list[trading212.Position]) -> list[dict[str, float | str]]:
    """Donut arcs. Percentages of invested value, largest first."""
    total = sum(p.value for p in positions)
    if total <= 0:
        return []
    out: list[dict[str, float | str]] = []
    offset = 25.0  # SVG arcs start at 3 o'clock; 25 rotates to 12.
    for i, p in enumerate(sorted(positions, key=lambda x: -x.value)):
        pct = p.value / total * 100
        out.append(
            {
                "ticker": p.ticker,
                "pct": round(pct, 2),
                "offset": round(offset, 2),
                "colour": SEGMENT_COLOURS[i % len(SEGMENT_COLOURS)],
            }
        )
        offset -= pct
    return out


def latest_note(session: Session) -> db.PortfolioNote | None:
    return session.exec(
        select(db.PortfolioNote).order_by(db.PortfolioNote.at.desc())
    ).first()


def view(session: Session) -> dict | None:
    """Panel data from the last sync. No network, no cost.

    Reads Holding rather than calling Trading212, because this runs on every
    dashboard render and the API is rate limited. Prices are as of synced_at,
    which the panel shows rather than hides.

    None rather than zeros when there is nothing: app/main.py already notes that
    placeholder numbers get read as real ones.
    """
    holdings = session.exec(select(db.Holding)).all()
    if not holdings:
        return None

    positions = [
        trading212.Position(
            ticker=h.ticker,
            quantity=h.quantity,
            avg_price=h.avg_price,
            current_price=h.current_price,
        )
        for h in holdings
    ]
    invested = sum(p.value for p in positions)
    cost = sum(p.cost for p in positions)
    note = latest_note(session)
    synced = max(h.synced_at for h in holdings)

    return {
        "segments": _segments(positions),
        "total_value": f"{invested:,.2f}",
        "day_change": ((invested / cost - 1) * 100) if cost else 0.0,
        "cash_free": f"as of {synced:%d %b %H:%M}",
        "market_note": (
            {"text": note.body, "url": None, "source": f"Opus, {note.at:%d %b %H:%M}"}
            if note
            else None
        ),
        "suggestions": None,
        "positions": [
            {"ticker": p.ticker, "value": f"{p.value:,.2f}", "pnl_pct": p.pnl_pct}
            for p in sorted(positions, key=lambda x: -x.value)
        ],
    }


def _render_positions(account: trading212.Account) -> str:
    lines = [
        f"- {p.ticker}: {p.quantity:g} units, avg {p.avg_price:,.2f}, "
        f"now {p.current_price:,.2f} ({p.pnl_pct:+.1f}%), value {p.value:,.2f}"
        for p in sorted(account.positions, key=lambda x: -x.value)
    ]
    return (
        f"Total value {account.total_value:,.2f}, free cash {account.cash_free:,.2f}, "
        f"day change {account.day_change_pct:+.2f}%.\n\nPositions:\n" + "\n".join(lines)
    )


def analyse(session: Session) -> db.PortfolioNote | None:
    """One Opus call. The only billed path in the app.

    Returns the stored note, or None if the portfolio could not be read or the
    call failed. Never raises into the request.
    """
    account = trading212.fetch()
    if account is None or not account.positions:
        log.info("portfolio analysis skipped: nothing to analyse")
        return None

    try:
        completion = remote.provider.complete(
            messages=[Message("user", _render_positions(account))],
            system=SYSTEM,
            model=config.MODEL_PORTFOLIO,
            effort="high",
            max_tokens=1024,
        )
    except LLMError:
        log.warning("portfolio analysis failed", exc_info=True)
        return None

    costs.record(
        "portfolio",
        "portfolio",
        completion.model,
        completion.cost_usd,
        auth="api_key",
        usage=completion.usage.as_dict(),
    )
    log.info(
        "portfolio analysis: $%.4f (%d in, %d out)",
        completion.cost_usd,
        completion.usage.input_tokens,
        completion.usage.output_tokens,
    )

    note = db.PortfolioNote(
        at=config.now(),
        body=completion.text,
        model=completion.model,
        cost_usd=completion.cost_usd,
    )
    session.add(note)
    session.commit()
    session.refresh(note)
    return note


def last_analysed(session: Session) -> datetime | None:
    note = latest_note(session)
    return note.at if note else None
