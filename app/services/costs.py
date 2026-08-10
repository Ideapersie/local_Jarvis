"""Spend tracking.

Every model call records what it cost. Without this, "is this too expensive"
can only be answered by guessing or by waiting for a bill.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlmodel import Session, func, select

from app import config, db

# Monthly Agent SDK credit by plan, for the dashboard's remaining-budget read.
PLAN_CREDITS = {"pro": 20.0, "max5": 100.0, "max20": 200.0, "none": 0.0}


def record(
    kind: str,
    tier: str,
    model: str,
    cost_usd: float | None,
    auth: str = "unknown",
    usage: dict[str, Any] | None = None,
) -> None:
    """Log one model call. Never raises - a failed cost write must not kill a turn."""
    usage = usage or {}
    try:
        with Session(db.engine) as s:
            s.add(
                db.CostLog(
                    at=config.now(),
                    kind=kind,
                    tier=tier,
                    model=model,
                    auth=auth,
                    cost_usd=float(cost_usd or 0.0),
                    input_tokens=int(usage.get("input_tokens") or 0),
                    output_tokens=int(usage.get("output_tokens") or 0),
                    cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
                )
            )
            s.commit()
    except Exception:  # pragma: no cover - logging must not break the caller
        import logging

        logging.getLogger("jarvis.costs").warning("cost log failed", exc_info=True)


def month_to_date(session: Session, today: date | None = None) -> dict[str, Any]:
    """Spend so far this calendar month, split by tier."""
    today = today or config.today()
    start = datetime(today.year, today.month, 1)

    rows = session.exec(select(db.CostLog).where(db.CostLog.at >= start)).all()

    total = sum(r.cost_usd for r in rows)
    by_tier: dict[str, float] = {}
    for r in rows:
        by_tier[r.tier] = by_tier.get(r.tier, 0.0) + r.cost_usd

    credit = PLAN_CREDITS.get(config.PLAN, 0.0)
    return {
        "total": total,
        "calls": len(rows),
        "by_tier": by_tier,
        "credit": credit,
        "remaining": max(0.0, credit - total) if credit else None,
        "pct": (min(total / credit, 1.0) * 100) if credit else None,
    }


def today_total(session: Session, day: date | None = None) -> float:
    day = day or config.today()
    start = datetime(day.year, day.month, day.day)
    total = session.exec(
        select(func.sum(db.CostLog.cost_usd)).where(db.CostLog.at >= start)
    ).one()
    return float(total or 0.0)
