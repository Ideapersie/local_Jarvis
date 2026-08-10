"""Chat panel: send over POST, stream the answer back over SSE.

Two endpoints rather than one because HTMX cannot stream a normal POST response.
The POST parks the message and hands back a bubble that opens an EventSource;
the GET runs the agent turn and streams into that bubble.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from html import escape

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from fastapi import APIRouter, Cookie, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, StreamingResponse

from app import agent, config, quick
from app.deps import templates
from app.services import costs

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

    Tries the quick tier first: a single Haiku call over a prefetched snapshot,
    roughly ninety times cheaper than an agent turn. It escalates itself when the
    question needs files, the web, or multi-step work, and any failure escalates
    too - a broken quick tier should cost a slower answer, never a wrong one.
    """
    answer, meta = await asyncio.to_thread(quick.try_answer, prompt)
    if answer is not None:
        log.info("quick tier answered (cost=$%.5f)", meta.get("cost_usd", 0.0))
        yield _sse("tool", '<span class="tool-line">&rarr; quick</span>')
        yield _sse("token", _render(answer))
        yield _sse("done", "")
        return

    log.info("escalating to agent tier: %s", meta.get("reason"))

    try:
        entry = await agent.get_entry(session_id)
    except Exception:
        log.exception("could not start agent session")
        yield _sse("token", "<em>Agent failed to start. Check the server log.</em>")
        yield _sse("done", "")
        return

    async with entry.lock:
        try:
            await entry.client.query(prompt)
            async for msg in entry.client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            yield _sse("token", _render(block.text))
                        elif isinstance(block, ToolUseBlock):
                            short = block.name.replace("mcp__jarvis__", "")
                            log.info("tool call: %s", block.name)
                            tag = escape(short)
                            yield _sse(
                                "tool",
                                f'<span class="tool-line">&rarr; {tag}</span>',
                            )
                elif isinstance(msg, ResultMessage):
                    auth = agent.active_auth()
                    costs.record(
                        "chat_agent",
                        "agent",
                        config.MODEL_AGENT,
                        msg.total_cost_usd,
                        auth=auth,
                        usage=msg.usage or {},
                    )
                    log.info(
                        "agent turn: cost=$%.4f turns=%d auth=%s",
                        msg.total_cost_usd or 0.0,
                        msg.num_turns,
                        auth,
                    )
                    # A spent plan credit stops requests dead. Detect it here so
                    # the next session silently uses the API key instead of the
                    # brief just not appearing one morning.
                    if msg.is_error and agent.looks_like_credit_exhaustion(
                        " ".join(msg.errors or []) + (msg.result or "")
                    ):
                        agent.note_credit_exhausted()
                    break
        except asyncio.CancelledError:
            # Browser closed the EventSource. Not an error.
            raise
        except Exception:
            log.exception("agent turn failed")
            yield _sse(
                "token", "<em>The agent errored mid-turn. Check the server log.</em>"
            )
        finally:
            entry.last_used = time.monotonic()

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
