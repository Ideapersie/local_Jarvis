"""Inbox triage: fetch, classify in one batched call, persist.

Quick tier, not the agent. Classifying forty subject lines is a fixed-shape task
with a fixed-shape answer, and one batched call with no tools is the cheapest
way to get it. That used to mean a tenth of a cent against several dollars of
agent turns; now that both run locally it means one prefill instead of forty.

Bodies are fetched only for mail the classifier flags as needing a reply. Most
inbox mail is a receipt or a newsletter, and there is no reason to put it
through a model.
"""

from __future__ import annotations

import json
import logging

from sqlmodel import Session, select

from app import config, db
from app.integrations import gmail
from app.llm import local
from app.llm.base import Message
from app.services import costs

log = logging.getLogger("jarvis.triage")

CATEGORIES = ["reply_needed", "fyi", "ignore", "linkedin", "whatsapp"]

SYSTEM = """You triage a job-hunting student's inbox. For each message, pick one
category.

reply_needed - a real person is waiting on a response, or there is a deadline,
  interview invitation, or application update that needs an action from him.
fyi - worth knowing, no action. Confirmations, receipts he should see, results.
ignore - marketing, newsletters, automated noise, social digests.
linkedin - a LinkedIn notification email.
whatsapp - a WhatsApp notification email.

Bias toward reply_needed only when someone is genuinely waiting. A recruiter
asking for availability is reply_needed. A job board digest is ignore.

Return one entry per message id you were given, and no others."""

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "category": {"type": "string", "enum": CATEGORIES},
                },
                "required": ["id", "category"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["items"],
    "additionalProperties": False,
}

WHY_SYSTEM = """Say in one short sentence what this email needs from the reader.
Start with a verb. No preamble, no greeting, no restating the subject line.
Example: "Confirm availability for a Thursday call." Under 15 words."""


def available() -> bool:
    """Triage needs a mailbox and a running model. Neither is assumed."""
    return gmail.available() and local.available()


def classify(messages: list[gmail.Message]) -> dict[str, str]:
    """Message id to category, in one call. Empty dict on failure."""
    if not messages:
        return {}

    listing = "\n".join(m.for_classifier() for m in messages)
    try:
        completion = local.provider.complete(
            messages=[Message("user", listing)],
            system=SYSTEM,
            # Enforced, not requested. Unconstrained, this model returns prose
            # and every message would fall into the unparseable branch below -
            # an inbox that silently triages nothing.
            schema=SCHEMA,
            max_tokens=2048,
        )
    except Exception:
        log.warning("triage classification failed", exc_info=True)
        return {}

    costs.record(
        "triage",
        "quick",
        config.LOCAL_MODEL,
        0.0,
        auth="local",
        usage=completion.usage.as_dict(),
    )

    try:
        items = json.loads(completion.text)["items"]
    except (json.JSONDecodeError, KeyError):
        log.warning("triage returned unparseable output")
        return {}

    known = {m.id for m in messages}
    return {
        item["id"]: item["category"]
        for item in items
        # Guard against hallucinated ids: a category attached to a message that
        # was never sent would be persisted as fact.
        if item.get("id") in known and item.get("category") in CATEGORIES
    }


def explain(message: gmail.Message) -> str | None:
    """One line on what a reply-needed message wants. None on failure."""
    body = gmail.fetch_body(message.id)
    if not body:
        return None

    try:
        completion = local.provider.complete(
            messages=[
                Message(
                    "user",
                    f"From: {message.sender}\nSubject: {message.subject}\n\n{body}",
                )
            ],
            system=WHY_SYSTEM,
            max_tokens=100,
        )
    except Exception:
        log.warning("triage explain failed for %s", message.id, exc_info=True)
        return None

    costs.record(
        "triage",
        "quick",
        config.LOCAL_MODEL,
        0.0,
        auth="local",
        usage=completion.usage.as_dict(),
    )
    return completion.text.strip() or None


def run(hours: int = 24) -> dict[str, int]:
    """Fetch, classify, persist. Returns a count per category."""
    if not available():
        log.info("triage skipped: google not configured")
        return {}

    messages = gmail.recent(hours=hours)
    if not messages:
        return {}

    # Platform notifications are identified by sender domain, which is exact -
    # no reason to spend a classification on them.
    preset = {m.id: m.platform for m in messages if m.platform}
    to_classify = [m for m in messages if not m.platform]

    categories = {**preset, **classify(to_classify)}
    if not categories:
        return {}

    counts: dict[str, int] = {}
    with Session(db.engine) as s:
        for msg in messages:
            category = categories.get(msg.id)
            if category is None:
                continue
            counts[category] = counts.get(category, 0) + 1

            existing = s.exec(
                select(db.Triage).where(db.Triage.message_id == msg.id)
            ).first()
            if existing is not None:
                # Already seen. Leave handled alone - re-running triage must not
                # resurface a thread the user has already dealt with.
                continue

            why = explain(msg) if category == "reply_needed" else None
            s.add(
                db.Triage(
                    message_id=msg.id,
                    thread_id=msg.thread_id,
                    sender=msg.sender,
                    sender_email=msg.sender_email,
                    subject=msg.subject,
                    category=category,
                    why=why,
                    seen_at=config.now(),
                )
            )
        s.commit()

    log.info("triage: %s", counts)
    return counts


def pending(session: Session, limit: int = 10) -> list[db.Triage]:
    """Unhandled reply-needed mail, newest first."""
    return list(
        session.exec(
            select(db.Triage)
            .where(db.Triage.category == "reply_needed")
            .where(db.Triage.handled == False)
            .order_by(db.Triage.seen_at.desc())
            .limit(limit)
        ).all()
    )


def summary(session: Session) -> dict[str, int]:
    """Unhandled counts by category, for the brief."""
    rows = session.exec(select(db.Triage).where(db.Triage.handled == False)).all()
    out: dict[str, int] = {}
    for r in rows:
        out[r.category] = out.get(r.category, 0) + 1
    return out
