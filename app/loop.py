"""The agentic loop, replacing claude-agent-sdk.

The SDK supplied the loop, the tools, the permission gate, the system prompt and
the skills. This is the loop and the wiring; the tools live in app/llm/ and
app/integrations/, and the guard lives inside the write tools themselves.

Why profiles instead of one big tool list. Measured on Qwen3.8-27B Q3_K_M, tool
selection is 84% with all eleven database tools in scope and every failure is
the same shape: any question containing "application N" plus an action verb
routes to get_applications instead of the write tool. Drop get_applications and
the same question is answered perfectly. It is a ranking failure, not a
capability one.

The fix is not a read/write router - the brief and the prep run both genuinely
need reads and writes in one turn. It is that each task knows what it needs, and
the profile simply leaves out the tool that causes the confusion. PREP_PROMPT
already passes the company, role, stage and date, so the prep profile has no
business carrying get_applications in the first place. Smaller profiles also cut
prefill, which is the dominant cost here: the full spec list is 1690 tokens and
about 133s uncached.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from app import config
from app.integrations import search
from app.llm import fs_tools, local
from app.llm import tools as db_tools
from app.llm.base import Completion, LLMError, Message
from app.services import costs

log = logging.getLogger("jarvis.loop")

MAX_ITERATIONS = 8
IDLE_TIMEOUT_S = 30 * 60
MAX_SESSIONS = 8


# --- tool profiles ----------------------------------------------------------

_DB_READS = frozenset(db_tools.READ_ONLY_NAMES)
_DB_WRITES = frozenset(db_tools.HANDLERS) - _DB_READS
_FS_READS = frozenset(fs_tools.READ_ONLY_NAMES)
_FS_WRITES = frozenset(fs_tools.WRITE_NAMES)
_SEARCH = frozenset({"web_search"})

PROFILES: dict[str, frozenset[str]] = {
    # Files only. brief_builder.gather() has already computed every number and
    # the prompt says so ("already gathered and correct, do not re-fetch"), so
    # the database tools would be 1690 tokens of prefill the job cannot use -
    # and a tempting way for it to contradict the facts it was handed.
    "brief": _FS_READS | _FS_WRITES,
    # The application's details are already in the prompt, so get_applications
    # is deliberately absent - it is the tool that swallowed every write intent
    # in the bench.
    "prep": frozenset({"get_prep_notes", "get_skills", "add_prep_note"})
    | _FS_READS
    | _SEARCH,
    # Also files only: the prompt names brain/goals.md, brain/weaknesses.md and
    # the last seven briefs, and nothing else.
    "reflection": _FS_READS | _FS_WRITES,
    # Chat that only needs to look things up.
    "chat_read": _DB_READS | _FS_READS | _SEARCH,
    # Chat that changes a record. No get_applications, same reason as prep.
    "chat_write": _DB_WRITES | _FS_READS | _FS_WRITES,
}

# Every tool, whichever module owns it.
_SYNC_HANDLERS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    **fs_tools.HANDLERS,
    "web_search": search.web_search,
}


def specs_for(profile: str) -> list[dict[str, Any]]:
    """OpenAI-format specs for one profile, in a stable order.

    Stable because the prefix cache is a byte match: reordering the tool list
    invalidates it and costs a full re-prefill.
    """
    names = PROFILES.get(profile)
    if names is None:
        raise ValueError(f"unknown tool profile: {profile}")
    out = [s for s in db_tools.specs() if s["function"]["name"] in names]
    out += fs_tools.specs(frozenset(names & set(fs_tools.SPECS)))
    if "web_search" in names:
        out.append(search.SPEC)
    return out


async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one tool, whichever module owns it. Never raises."""
    if name in db_tools.HANDLERS:
        return await db_tools.dispatch(name, args)
    handler = _SYNC_HANDLERS.get(name)
    if handler is None:
        log.warning("model called unknown tool: %s", name)
        return {
            "content": [{"type": "text", "text": f"No such tool: {name}"}],
            "is_error": True,
        }
    # Filesystem and network work off the event loop so a slow search does not
    # stall the SSE stream feeding the browser.
    return await asyncio.to_thread(handler, args)


def _result_text(result: dict[str, Any]) -> str:
    parts = [b.get("text", "") for b in result.get("content", [])]
    return "\n".join(p for p in parts if p)


# --- prompts ----------------------------------------------------------------


@lru_cache(maxsize=1)
def system_prompt() -> str:
    """The agent's instructions, from .claude/CLAUDE.md.

    The SDK loaded this via setting_sources=["project"]. Read it from disk so
    the file stays the single source of truth and editing it does not require a
    code change.
    """
    md = config.ROOT / ".claude" / "CLAUDE.md"
    if not md.exists():
        log.warning("no .claude/CLAUDE.md; the agent will have no project instructions")
        return "You are Jarvis, a personal dashboard assistant. Be direct and brief."
    return md.read_text(encoding="utf-8")


@lru_cache(maxsize=8)
def skill(name: str) -> str:
    """One skill's instructions, from .claude/skills/<name>/SKILL.md."""
    path = config.ROOT / ".claude" / "skills" / name / "SKILL.md"
    if not path.exists():
        log.warning("no skill named %r", name)
        return ""
    return path.read_text(encoding="utf-8")


def build_system(profile: str, skill_name: str | None = None) -> str:
    parts = [system_prompt()]
    if skill_name:
        body = skill(skill_name)
        if body:
            parts.append(f"## Skill: {skill_name}\n\n{body}")
    parts.append(
        "## Tools\n"
        "Call a tool whenever the answer depends on the database or on a file. "
        "Do not guess a number you could look up. When the user tells you "
        "something changed, record it rather than only reading it back. "
        "You may write only to brain/ and briefs/."
    )
    return "\n\n".join(parts)


# --- events -----------------------------------------------------------------


@dataclass(slots=True)
class Event:
    kind: str  # "text" | "tool" | "done" | "error"
    data: str = ""


# --- session registry -------------------------------------------------------


@dataclass(slots=True)
class _Entry:
    history: list[Message]
    lock: asyncio.Lock
    last_used: float


_sessions: dict[str, _Entry] = {}
_registry_lock = asyncio.Lock()


async def get_entry(session_id: str) -> _Entry:
    """Conversation state for a session.

    Much lighter than the SDK version: there is no subprocess to leak, so this
    bounds memory rather than processes. The per-session lock still matters -
    a double submit from one browser would otherwise interleave two turns.
    """
    async with _registry_lock:
        entry = _sessions.get(session_id)
        if entry is not None:
            entry.last_used = time.monotonic()
            return entry
        if len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions, key=lambda k: _sessions[k].last_used)
            log.info("session cap reached, evicting %s", oldest)
            _sessions.pop(oldest, None)
        entry = _Entry(history=[], lock=asyncio.Lock(), last_used=time.monotonic())
        _sessions[session_id] = entry
        return entry


async def reap_idle() -> int:
    async with _registry_lock:
        cutoff = time.monotonic() - IDLE_TIMEOUT_S
        stale = [k for k, e in _sessions.items() if e.last_used < cutoff]
        for key in stale:
            log.info("reaping idle session: %s", key)
            _sessions.pop(key, None)
        return len(stale)


async def shutdown_all() -> None:
    async with _registry_lock:
        _sessions.clear()
    log.info("all sessions cleared")


async def reaper_loop(interval_s: int = 300) -> None:
    while True:
        try:
            await asyncio.sleep(interval_s)
            await reap_idle()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("reaper iteration failed")


def live_session_count() -> int:
    return len(_sessions)


# --- the loop ---------------------------------------------------------------


async def _complete(**kwargs: Any) -> Completion:
    """The provider call is blocking; keep it off the event loop."""
    return await asyncio.to_thread(local.provider.complete, **kwargs)


async def run_turn(
    session_id: str,
    prompt: str,
    *,
    profile: str = "chat_read",
    skill_name: str | None = None,
    kind: str = "chat",
    think: bool | None = None,
    max_tokens: int = 2048,
) -> AsyncIterator[Event]:
    """One turn: call, run any tools, repeat until the model stops asking.

    Yields events as they happen so the caller can stream. Tool results go back
    in a single message per round, which is what keeps the model willing to ask
    for several at once.
    """
    entry = await get_entry(session_id)
    system = build_system(profile, skill_name)
    tools = specs_for(profile)

    async with entry.lock:
        entry.history.append(Message("user", prompt))
        in_tokens = out_tokens = 0

        try:
            for _ in range(MAX_ITERATIONS):
                completion = await _complete(
                    messages=entry.history,
                    system=system,
                    tools=tools or None,
                    max_tokens=max_tokens,
                    think=think,
                )
                in_tokens += completion.usage.input_tokens
                out_tokens += completion.usage.output_tokens

                if completion.text:
                    yield Event("text", completion.text)

                if not completion.wants_tools:
                    entry.history.append(Message("assistant", completion.text))
                    break

                entry.history.append(
                    Message(
                        "assistant", completion.text, tool_calls=completion.tool_calls
                    )
                )

                results = await asyncio.gather(
                    *(dispatch(tc.name, tc.arguments) for tc in completion.tool_calls)
                )
                for call, result in zip(completion.tool_calls, results, strict=True):
                    log.info(
                        "tool %s -> %s",
                        call.name,
                        "error" if result.get("is_error") else "ok",
                    )
                    yield Event("tool", call.name)
                    entry.history.append(
                        Message("tool", _result_text(result), tool_call_id=call.id)
                    )
            else:
                # Ran out of iterations rather than finishing. Say so: a silent
                # truncation looks like the model simply stopped caring.
                log.warning("turn hit the %d iteration cap", MAX_ITERATIONS)
                yield Event("error", "I ran out of steps before finishing that.")

        except LLMError as exc:
            log.warning("model call failed: %s", exc)
            yield Event(
                "error", "The local model is not responding. Is llama-server running?"
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("turn failed")
            yield Event("error", "That turn errored. Check the server log.")
        finally:
            entry.last_used = time.monotonic()
            costs.record(
                kind,
                "local",
                config.LOCAL_MODEL,
                0.0,
                auth="local",
                usage={"input_tokens": in_tokens, "output_tokens": out_tokens},
            )

    yield Event("done")


async def run_to_text(
    session_id: str,
    prompt: str,
    *,
    profile: str = "chat_read",
    skill_name: str | None = None,
    kind: str = "chat",
    think: bool | None = None,
    max_tokens: int = 2048,
) -> str:
    """run_turn for callers that want the final answer, not a stream.

    Returns the last text block. The brief writer wants the closing summary, not
    the agent's narration of what it was about to do - joining every block put
    "Now invoking the brief-writer skill" into the dashboard panel.
    """
    texts: list[str] = []
    async for event in run_turn(
        session_id,
        prompt,
        profile=profile,
        skill_name=skill_name,
        kind=kind,
        think=think,
        max_tokens=max_tokens,
    ):
        if event.kind == "text" and event.data.strip():
            texts.append(event.data.strip())
        elif event.kind == "error":
            log.warning("run_to_text saw an error event: %s", event.data)
    return texts[-1] if texts else ""


def answer_json(
    prompt: str, schema: dict[str, Any], *, system: str | None = None
) -> Completion:
    """One constrained call, no tools and no loop.

    The quick tier's shape. Unconstrained this model scored 0/6 against
    quick.RESPONSE_SCHEMA, so the schema is enforced rather than requested.
    """
    return local.provider.complete(
        messages=[Message("user", prompt)],
        system=system,
        schema=schema,
        max_tokens=1024,
    )
