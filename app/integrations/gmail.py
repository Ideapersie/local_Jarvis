"""Gmail reads for inbox triage.

Headers first, bodies only for what the classifier flags as needing a reply.
Fetching every body up front would be slower and would put a lot of private mail
through a model for no reason.
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass, field

from app.integrations import google_auth

log = logging.getLogger("jarvis.gmail")

MAX_MESSAGES = 40
BODY_CHARS = 1500

# Senders whose mail is a notification about another platform. Spec section 8:
# LinkedIn and WhatsApp have no usable read API, and scraping them risks the
# account during a job hunt - but both email you, so route the notifications.
PLATFORM_SENDERS = {
    "linkedin": ("linkedin.com",),
    "whatsapp": ("whatsapp.com",),
}


@dataclass
class Message:
    id: str
    thread_id: str
    sender: str
    sender_email: str
    subject: str
    snippet: str
    platform: str | None = None
    body: str = field(default="", repr=False)

    def for_classifier(self) -> str:
        line = f"[{self.id}] from {self.sender}: {self.subject}"
        if self.snippet:
            line += f" | {self.snippet[:160]}"
        return line


def available() -> bool:
    return google_auth.configured()


def _header(headers: list[dict], name: str) -> str:
    lower = name.lower()
    return next(
        (h.get("value", "") for h in headers if h.get("name", "").lower() == lower), ""
    )


def _split_sender(raw: str) -> tuple[str, str]:
    """'Name <a@b.com>' into ('Name', 'a@b.com'). Falls back to the raw string."""
    match = re.match(r"^\s*(.*?)\s*<([^>]+)>\s*$", raw)
    if match:
        name = match.group(1).strip().strip('"') or match.group(2)
        return name, match.group(2).lower()
    return raw.strip(), raw.strip().lower()


def _platform_for(email: str) -> str | None:
    for platform, domains in PLATFORM_SENDERS.items():
        if any(d in email for d in domains):
            return platform
    return None


def recent(hours: int = 24, limit: int = MAX_MESSAGES) -> list[Message]:
    """Headers for inbox mail from the last `hours`. Never raises."""
    try:
        svc = google_auth.service("gmail", "v1")
        listing = (
            svc.users()
            .messages()
            .list(
                userId="me",
                q=f"in:inbox newer_than:{max(1, hours // 24)}d",
                maxResults=limit,
            )
            .execute()
        )
    except Exception:
        log.warning("gmail list failed", exc_info=True)
        return []

    out: list[Message] = []
    for stub in listing.get("messages", []):
        try:
            msg = (
                svc.users()
                .messages()
                .get(
                    userId="me",
                    id=stub["id"],
                    format="metadata",
                    metadataHeaders=["From", "Subject", "Date"],
                )
                .execute()
            )
        except Exception:
            log.warning("gmail get failed for %s", stub["id"], exc_info=True)
            continue

        headers = msg.get("payload", {}).get("headers", [])
        name, email = _split_sender(_header(headers, "From"))
        out.append(
            Message(
                id=msg["id"],
                thread_id=msg.get("threadId", msg["id"]),
                sender=name,
                sender_email=email,
                subject=_header(headers, "Subject") or "(no subject)",
                snippet=msg.get("snippet", ""),
                platform=_platform_for(email),
            )
        )
    return out


def _walk_parts(payload: dict) -> str:
    """Depth-first search for text/plain, falling back to text/html."""
    mime = payload.get("mimeType", "")
    data = payload.get("body", {}).get("data")

    if mime == "text/plain" and data:
        return _decode(data)

    for part in payload.get("parts", []) or []:
        found = _walk_parts(part)
        if found:
            return found

    if mime == "text/html" and data:
        return re.sub(r"<[^>]+>", " ", _decode(data))
    return ""


def _decode(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def fetch_body(message_id: str) -> str:
    """Plain-text body, truncated. Only called for reply-needed mail."""
    try:
        svc = google_auth.service("gmail", "v1")
        msg = (
            svc.users()
            .messages()
            .get(userId="me", id=message_id, format="full")
            .execute()
        )
    except Exception:
        log.warning("gmail body fetch failed for %s", message_id, exc_info=True)
        return ""

    text = _walk_parts(msg.get("payload", {}))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:BODY_CHARS]
