"""Chat panel: send over POST, stream the answer back over SSE.

Two endpoints rather than one because HTMX cannot stream a normal POST response.
The POST parks the message and hands back a bubble that opens an EventSource;
the GET runs the agent turn and streams into that bubble.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from html import escape

from fastapi import APIRouter, Cookie, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from app import loop, quick
from app.deps import templates

log = logging.getLogger("jarvis.chat")

router = APIRouter(prefix="/chat", tags=["chat"])

SESSION_COOKIE = "jarvis_session"

# Messages waiting for their stream to be opened. Bounded so an abandoned POST
# that never opens its EventSource cannot grow this without limit.
_pending: dict[str, tuple[str, str]] = {}
MAX_PENDING = 32


_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_CODE = re.compile(r"`([^`\n]+)`")


def _render(text: str) -> str:
    """Escape, then apply the two bits of markdown the agent actually uses.

    Order matters: escaping first means the captured groups contain no live
    markup, so the tags added here are the only HTML in the output. Doing it the
    other way round would let model output inject elements.
    """
    out = escape(text)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    return _CODE.sub(r"<code>\1</code>", out)


def _sse(event: str, data: str) -> str:
    """Frame one SSE event.

    Newlines are the frame delimiter, so a payload containing one must be split
    across several `data:` lines - the browser rejoins them with \\n. Emitting
    raw newlines here silently truncates the message at the first one.
    """
    body = "\n".join(f"data: {line}" for line in data.split("\n"))
    return f"event: {event}\n{body}\n\n"


@router.post("/send", response_class=HTMLResponse)
async def send(
    request: Request,
    response: Response,
    message: str = Form(...),
    jarvis_session: str | None = Cookie(default=None),
):
    """Park the message, return the user bubble plus an empty assistant bubble."""
    message = message.strip()
    if not message:
        return HTMLResponse("")

    session_id = jarvis_session or uuid.uuid4().hex
    msg_id = uuid.uuid4().hex

    if len(_pending) >= MAX_PENDING:
        oldest = next(iter(_pending))
        _pending.pop(oldest, None)
    _pending[msg_id] = (session_id, message)

    html = templates.get_template("partials/chat_exchange.html").render(
        request=request, user_text=message, msg_id=msg_id
    )
    out = HTMLResponse(html)
    # Same-site cookie so the session id is not carried on cross-site requests.
    out.set_cookie(
        SESSION_COOKIE,
        session_id,
        httponly=True,
        samesite="strict",
        max_age=60 * 60 * 24 * 30,
    )
    return out


async def _run_turn(session_id: str, prompt: str):
    """Answer one message, cheaply if possible.

    Tries the quick tier first: one constrained call over a prefetched snapshot,
    no tools. That is 175 tokens of context against 1379 for the smallest
    tool-bearing profile, and prefill runs at about 14.5 tok/s here - roughly a
    second versus a minute and a half. It escalates itself when the question
    needs files, the web, or multi-step work, and any failure escalates too: a
    broken quick tier should cost a slower answer, never a wrong one.
    """
    answer, meta = await asyncio.to_thread(quick.try_answer, prompt)
    if answer is not None:
        log.info("quick tier answered")
        yield _sse("tool", '<span class="tool-line">&rarr; quick</span>')
        yield _sse("token", _render(answer))
        yield _sse("done", "")
        return

    log.info("escalating to the agent loop: %s", meta.get("reason"))

    # Read tools only. A chat turn that changes a record goes through the
    # career panel, which is explicit about what it is writing; letting an
    # open-ended chat message reach the write tools would put a fabricated
    # stage change one ambiguous sentence away.
    async for event in loop.run_turn(
        session_id, prompt, profile="chat_read", kind="chat_agent"
    ):
        if event.kind == "text" and event.data:
            yield _sse("token", _render(event.data))
        elif event.kind == "tool":
            yield _sse(
                "tool", f'<span class="tool-line">&rarr; {escape(event.data)}</span>'
            )
        elif event.kind == "error":
            yield _sse("token", f"<em>{escape(event.data)}</em>")

    yield _sse("done", "")


@router.get("/stream/{msg_id}")
async def stream(msg_id: str):
    parked = _pending.pop(msg_id, None)
    if parked is None:
        raise HTTPException(status_code=404, detail="no such pending message")
    session_id, prompt = parked

    return StreamingResponse(
        _run_turn(session_id, prompt),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Harmless locally; prevents buffering if this ever sits behind a proxy.
            "X-Accel-Buffering": "no",
        },
    )
