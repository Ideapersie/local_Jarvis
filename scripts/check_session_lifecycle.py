"""Manual check: does a chat session's subprocess get reclaimed?

Each ClaudeSDKClient owns a bundled claude.exe. The registry is only safe if
disconnect actually reaps it - a leak here leaks processes, not just memory.

Run directly:  .venv/Scripts/python.exe scripts/check_session_lifecycle.py
Not a pytest test: it spawns a real subprocess and costs an API call.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

# Run from anywhere: put the repo root on the path rather than requiring cwd.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PS = (
    '(Get-CimInstance Win32_Process -Filter "ParentProcessId={pid}" '
    "| Measure-Object).Count"
)


def child_count() -> int:
    if sys.platform != "win32":
        return -1
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", PS.format(pid=os.getpid())],
        capture_output=True,
        text=True,
    )
    try:
        return int(out.stdout.strip())
    except ValueError:
        return -1


async def main() -> int:
    from app import agent

    start = child_count()
    print(f"children at start       : {start}")

    entry = await agent.get_entry("lifecycle-check")
    await entry.client.query("Say OK and nothing else.")
    async for _ in entry.client.receive_response():
        pass

    during = child_count()
    print(
        f"children with session   : {during}  (registry: {agent.live_session_count()})"
    )

    await agent.shutdown_all()
    await asyncio.sleep(2)

    after = child_count()
    print(
        f"children after shutdown : {after}  (registry: {agent.live_session_count()})"
    )

    ok = during > start and after == start and agent.live_session_count() == 0
    print("\nRESULT:", "PASS - subprocess reclaimed" if ok else "FAIL - leaked")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
