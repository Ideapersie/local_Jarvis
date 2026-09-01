"""Trading212 positions and cash.

Read-only. This module never places an order and has no code path that could:
the two endpoints it calls are both GETs, and nothing here takes an amount.

Defaults to the demo environment (config.TRADING212_BASE_URL), which is what
.env.example ships. Pointing it at the live account is a deliberate edit.

Returns None rather than zeros when it cannot reach the API. app/main.py already
carries the reason in a comment: placeholder numbers get read as real ones, and
a portfolio panel showing a confident 0.00 is worse than one showing nothing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from app import config

log = logging.getLogger("jarvis.trading212")

TIMEOUT_S = 20.0


@dataclass(slots=True)
class Position:
    ticker: str
    quantity: float
    avg_price: float
    current_price: float

    @property
    def value(self) -> float:
        return self.quantity * self.current_price

    @property
    def cost(self) -> float:
        return self.quantity * self.avg_price

    @property
    def pnl_pct(self) -> float:
        return (
            ((self.current_price / self.avg_price) - 1) * 100 if self.avg_price else 0.0
        )


@dataclass(slots=True)
class Account:
    positions: list[Position]
    cash_free: float
    total_value: float
    day_change_pct: float


def available() -> bool:
    return bool(config.TRADING212_API_KEY)


def _get(path: str) -> object | None:
    url = f"{config.TRADING212_BASE_URL.rstrip('/')}{path}"
    try:
        r = httpx.get(
            url,
            headers={"Authorization": config.TRADING212_API_KEY or ""},
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as exc:
        log.warning("trading212 %s failed: %s", path, exc)
        return None


def fetch() -> Account | None:
    """Current positions and cash, or None if the API cannot be reached."""
    if not available():
        return None

    raw_positions = _get("/equity/portfolio")
    raw_cash = _get("/equity/account/cash")
    if raw_positions is None or raw_cash is None:
        return None

    positions: list[Position] = []
    for p in raw_positions if isinstance(raw_positions, list) else []:
        try:
            positions.append(
                Position(
                    ticker=str(p.get("ticker", "?")),
                    quantity=float(p.get("quantity") or 0),
                    avg_price=float(p.get("averagePrice") or 0),
                    current_price=float(p.get("currentPrice") or 0),
                )
            )
        except (TypeError, ValueError):
            log.warning("skipping a malformed position: %r", p)

    cash = raw_cash if isinstance(raw_cash, dict) else {}
    try:
        free = float(cash.get("free") or 0)
        total = float(cash.get("total") or 0)
        ppl = float(cash.get("ppl") or 0)
    except (TypeError, ValueError):
        log.warning("malformed cash payload: %r", cash)
        return None

    invested = sum(p.cost for p in positions)
    day_change = (ppl / invested * 100) if invested else 0.0

    return Account(
        positions=positions,
        cash_free=free,
        total_value=total or (free + sum(p.value for p in positions)),
        day_change_pct=day_change,
    )
