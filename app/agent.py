"""Agent SDK wrapper: options, permission gate, and the session registry.

Every client owns a Claude Code CLI subprocess. The registry is therefore
bounded and reaped: leaking here leaks processes, not just memory, and an
orphaned subprocess outlives the request that made it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from app import config
from app.agent_tools import TOOL_NAMES, jarvis_server

log = logging.getLogger("jarvis.agent")

# Only these two trees are writable. brain/ is git-tracked, which is what makes
# free writes recoverable; briefs/ is append-only by convention.
WRITABLE = (config.BRAIN_DIR, config.BRIEFS_DIR)

WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}

IDLE_TIMEOUT_S = 30 * 60
MAX_SESSIONS = 8
MAX_BUDGET_USD = 2.00


# --- permission gate --------------------------------------------------------


def _is_writable(raw_path: str) -> bool:
    """True only if the resolved path sits inside brain/ or briefs/.

    Resolution happens before the check so `../` and symlinks cannot walk out.
    Comparing the raw string would let `brain/../app/main.py` through.
    """
    try:
        target = Path(raw_path)
        if not target.is_absolute():
            target = config.ROOT / target
        target = target.resolve()
    except (OSError, ValueError):
        return False
    return any(target.is_relative_to(allowed.resolve()) for allowed in WRITABLE)


async def can_use_tool(
    tool_name: str, tool_input: dict[str, Any], context: ToolPermissionContext
) -> PermissionResultAllow | PermissionResultDeny:
    """Confine writes to brain/ and briefs/. Reads are unrestricted."""
    if tool_name not in WRITE_TOOLS:
        return PermissionResultAllow(updated_input=tool_input)

    path = tool_input.get("file_path") or tool_input.get("path") or ""
    if _is_writable(str(path)):
        return PermissionResultAllow(updated_input=tool_input)

    log.warning("denied %s outside writable tree: %s", tool_name, path)
    return PermissionResultDeny(
        message=(
            f"Writing to {path} is not permitted. You may only write to "
            f"brain/ and briefs/."
        )
    )


def build_options() -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        # Pinned to the repo. The agent must never see the home directory.
        cwd=str(config.ROOT),
        # Without this, .claude/ is silently ignored and the agent behaves
        # almost right, which is harder to debug than an outright failure.
        setting_sources=["project"],
        model=config.MODEL_AGENT,
        mcp_servers={"jarvis": jarvis_server},
        # Write and Edit are deliberately absent. An allowed_tools entry
        # auto-approves a tool *before* can_use_tool is consulted, so listing
        # them here would silently disable the path confinement below - the SDK
        # warns about exactly this shadowing. Leaving them out makes writes fall
        # through to the callback, which is the only thing keeping the agent
        # inside brain/ and briefs/. Reads are safe to auto-approve.
        allowed_tools=["Read", "Glob", "Grep", *TOOL_NAMES],
        # Bare name removes Bash from context entirely, so the agent never
        # wastes a turn attempting it. It arrives on Day 3 with the brief.
        disallowed_tools=["Bash"],
        can_use_tool=can_use_tool,
        max_budget_usd=MAX_BUDGET_USD,
    )


# --- session registry -------------------------------------------------------


class _Entry:
    __slots__ = ("client", "last_used", "lock")

    def __init__(self, client: ClaudeSDKClient) -> None:
        self.client = client
        self.last_used = time.monotonic()
        # Serialises turns within one session: the SDK client is not safe to
        # drive concurrently, and a double-submit from one browser would
        # otherwise interleave two turns on the same subprocess.
        self.lock = asyncio.Lock()


_sessions: dict[str, _Entry] = {}
_registry_lock = asyncio.Lock()


async def get_entry(session_id: str) -> _Entry:
    """Return the live client for a session, creating one if needed."""
    async with _registry_lock:
        entry = _sessions.get(session_id)
        if entry is not None:
            entry.last_used = time.monotonic()
            return entry

        if len(_sessions) >= MAX_SESSIONS:
            oldest = min(_sessions, key=lambda k: _sessions[k].last_used)
            log.info("session cap reached, evicting %s", oldest)
            await _close(oldest)

        client = ClaudeSDKClient(options=build_options())
        await client.connect()
        entry = _Entry(client)
        _sessions[session_id] = entry
        log.info("agent session started: %s (%d live)", session_id, len(_sessions))
        return entry


async def _close(session_id: str) -> None:
    """Drop one session. Assumes the caller holds _registry_lock."""
    entry = _sessions.pop(session_id, None)
    if entry is None:
        return
    try:
        await entry.client.disconnect()
    except Exception:
        # A subprocess that already died is fine; losing the registry entry is
        # what matters, so never let this propagate.
        log.warning("disconnect failed for %s", session_id, exc_info=True)


async def reap_idle() -> int:
    async with _registry_lock:
        cutoff = time.monotonic() - IDLE_TIMEOUT_S
        stale = [k for k, e in _sessions.items() if e.last_used < cutoff]
        for key in stale:
            log.info("reaping idle agent session: %s", key)
            await _close(key)
        return len(stale)


async def shutdown_all() -> None:
    async with _registry_lock:
        for key in list(_sessions):
            await _close(key)
    log.info("all agent sessions closed")


async def reaper_loop(interval_s: int = 300) -> None:
    """Background task started from the FastAPI lifespan."""
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
