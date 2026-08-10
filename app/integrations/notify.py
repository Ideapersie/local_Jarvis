"""Reminder delivery.

Telegram when a token is configured, otherwise the log plus a Reminder row the
dashboard surfaces. One function to swap, so the 18:00 check is testable today
and gets real delivery the moment a token lands in .env.

Every reminder is persisted either way. A log line scrolls away; a row lets you
answer "did it actually fire" tomorrow.
"""

from __future__ import annotations

import logging

import httpx
from sqlmodel import Session

from app import config, db

log = logging.getLogger("jarvis.notify")

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"


def telegram_configured() -> bool:
    return bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_CHAT_ID)


def _send_telegram(body: str) -> bool:
    try:
        r = httpx.post(
            TELEGRAM_API.format(token=config.TELEGRAM_BOT_TOKEN),
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": body,
                "disable_notification": False,
            },
            timeout=15.0,
        )
        r.raise_for_status()
        return True
    except Exception:
        log.warning("telegram send failed, falling back to log", exc_info=True)
        return False


def send(body: str, kind: str = "manual") -> str:
    """Deliver one reminder. Returns the channel actually used.

    Never raises: a failed reminder must not take down the scheduled job that
    produced it.
    """
    via = "log"
    if telegram_configured() and _send_telegram(body):
        via = "telegram"
    else:
        log.info("REMINDER (%s): %s", kind, body)

    try:
        with Session(db.engine) as s:
            s.add(db.Reminder(at=config.now(), kind=kind, body=body, delivered_via=via))
            s.commit()
    except Exception:
        log.warning("could not persist reminder", exc_info=True)

    return via


def unseen(session: Session, limit: int = 5) -> list[db.Reminder]:
    """Recent undismissed reminders, newest first, for the Urgent card."""
    from sqlmodel import select

    return list(
        session.exec(
            select(db.Reminder)
            .where(db.Reminder.seen == False)
            .order_by(db.Reminder.at.desc())
            .limit(limit)
        ).all()
    )
