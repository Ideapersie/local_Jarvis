"""Filesystem tools, and the guard that keeps the agent inside brain/.

claude-agent-sdk supplied Read/Glob/Grep/Write/Edit and a can_use_tool callback
that confined writes. None of that comes with a local model, so it is rebuilt
here - including the part that is easy to get wrong.

The lesson from scripts/check_write_confinement.py is worth repeating, because
it already happened once on this project: a correct guard that never runs is
indistinguishable from no guard. Under the SDK an allowed_tools entry
auto-approved writes before the callback fired. The structural fix here is that
_check_write is called inside write_file and edit_file themselves, not by the
caller, so there is no dispatch path that reaches a write without passing it.

Reads are confined too, which the SDK setup did not really manage. It pinned cwd
to the repo but an absolute path could still walk out. .claude/settings.json
denied reading token.json and credentials.json; that list is honoured here and
extended with .env, which holds ANTHROPIC_API_KEY and the Trading212 secrets and
was previously readable. An agent that reads .env and quotes it into a brief has
just written a credential into git.
"""

from __future__ import annotations

import fnmatch
import logging
import re
from pathlib import Path
from typing import Any

from app import config

log = logging.getLogger("jarvis.llm.fs")

# The only two writable trees. brain/ is git-tracked in the private repo, which
# is what makes free writes reviewable and revertible.
WRITABLE = (config.BRAIN_DIR, config.BRIEFS_DIR)

# Readable tree. Everything the agent may look at lives under the repo.
READ_ROOT = config.ROOT

# Never readable, by exact filename anywhere in the tree. Secrets, not policy.
DENIED_NAMES = frozenset({".env", "credentials.json", "token.json"})

MAX_READ_BYTES = 200_000
MAX_MATCHES = 200

# Never walked. .venv alone is tens of thousands of files, and a listing that
# buries brain/goals.md under site-packages is useless to the model as well as
# slow to produce.
SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".pytest_cache",
        ".ruff_cache",
    }
)


def _skipped(rel: str) -> bool:
    return any(part in SKIP_DIRS for part in Path(rel).parts)


def _text(body: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": body}]}


def _error(body: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": body}], "is_error": True}


def _resolve(raw: str) -> Path | None:
    """Absolute, symlink-resolved path, or None if it cannot be resolved.

    Resolution happens before any comparison. Comparing raw strings would let
    brain/../app/main.py through, which is the classic escape.
    """
    try:
        target = Path(raw)
        if not target.is_absolute():
            target = config.ROOT / target
        return target.resolve()
    except (OSError, ValueError):
        return None


def is_writable(raw: str) -> bool:
    """True only if the resolved path sits inside brain/ or briefs/."""
    target = _resolve(raw)
    if target is None:
        return False
    if target.name in DENIED_NAMES:
        return False
    return any(target.is_relative_to(root.resolve()) for root in WRITABLE)


def is_readable(raw: str) -> bool:
    """True if the resolved path is inside the repo or brain/, and is not a secret.

    The writable trees have to be named explicitly, not assumed to sit under the
    repo. brain/ and briefs/ are directory junctions into the private
    jarvis-brain repo, so they resolve OUTSIDE local_Jarvis. Checking the repo
    root alone denied brain/goals.md, which the brief writer is told to read on
    every run - the tests caught it, the dashboard would have shown it as the
    brief mysteriously ignoring the user's goals.
    """
    target = _resolve(raw)
    if target is None:
        return False
    if target.name in DENIED_NAMES:
        return False
    roots = [READ_ROOT.resolve()] + [w.resolve() for w in WRITABLE]
    return any(target.is_relative_to(root) for root in roots)


def _check_write(path: str) -> dict[str, Any] | None:
    if is_writable(path):
        return None
    log.warning("denied write outside the permitted tree: %s", path)
    return _error(
        f"Writing to {path} is not permitted. You may only write to brain/ and briefs/."
    )


def _check_read(path: str) -> dict[str, Any] | None:
    if is_readable(path):
        return None
    log.warning("denied read: %s", path)
    return _error(f"Reading {path} is not permitted.")


# --- the tools --------------------------------------------------------------


def read_file(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path", "")).strip()
    if denied := _check_read(path):
        return denied
    target = _resolve(path)
    if target is None or not target.is_file():
        return _error(f"No such file: {path}")
    try:
        raw = target.read_bytes()[:MAX_READ_BYTES]
        body = raw.decode("utf-8", errors="replace")
    except OSError as exc:
        return _error(f"Could not read {path}: {exc}")
    truncated = target.stat().st_size > MAX_READ_BYTES
    return _text(body + ("\n\n[truncated]" if truncated else ""))


def list_files(args: dict[str, Any]) -> dict[str, Any]:
    pattern = str(args.get("pattern", "*")).strip() or "*"
    try:
        hits = []
        for p in config.ROOT.rglob("*"):
            rel = str(p.relative_to(config.ROOT)).replace("\\", "/")
            if _skipped(rel) or not p.is_file() or p.name in DENIED_NAMES:
                continue
            if fnmatch.fnmatch(rel, pattern):
                hits.append(rel)
        hits.sort()
    except (OSError, ValueError) as exc:
        return _error(f"Could not list files: {exc}")
    if not hits:
        return _text(f"No files match {pattern}.")
    shown = hits[:MAX_MATCHES]
    extra = f"\n\n[{len(hits) - len(shown)} more]" if len(hits) > len(shown) else ""
    return _text("\n".join(shown) + extra)


def search(args: dict[str, Any]) -> dict[str, Any]:
    pattern = str(args.get("pattern", ""))
    glob = str(args.get("glob", "**/*.md")).strip() or "**/*.md"
    if not pattern:
        return _error("search needs a pattern.")
    try:
        rx = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return _error(f"Bad regex: {exc}")

    out: list[str] = []
    for p in sorted(config.ROOT.glob(glob)):
        rel_check = str(p.relative_to(config.ROOT)).replace("\\", "/")
        if _skipped(rel_check) or not p.is_file() or p.name in DENIED_NAMES:
            continue
        try:
            for n, line in enumerate(
                p.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                if rx.search(line):
                    rel = str(p.relative_to(config.ROOT)).replace("\\", "/")
                    out.append(f"{rel}:{n}: {line.strip()[:200]}")
                    if len(out) >= MAX_MATCHES:
                        return _text("\n".join(out) + "\n\n[more matches truncated]")
        except OSError:
            continue
    return _text("\n".join(out) if out else f"No matches for {pattern}.")


def write_file(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path", "")).strip()
    content = str(args.get("content", ""))
    if denied := _check_write(path):
        return denied
    target = _resolve(path)
    if target is None:
        return _error(f"Bad path: {path}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    except OSError as exc:
        return _error(f"Could not write {path}: {exc}")
    log.info("agent wrote %s (%d bytes)", path, len(content))
    return _text(f"Wrote {path} ({len(content)} bytes).")


def append_file(args: dict[str, Any]) -> dict[str, Any]:
    """Append rather than replace. The memory protocol prefers this."""
    path = str(args.get("path", "")).strip()
    content = str(args.get("content", ""))
    if denied := _check_write(path):
        return denied
    target = _resolve(path)
    if target is None:
        return _error(f"Bad path: {path}")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as fh:
            fh.write(content if content.endswith("\n") else content + "\n")
    except OSError as exc:
        return _error(f"Could not append to {path}: {exc}")
    log.info("agent appended to %s (%d bytes)", path, len(content))
    return _text(f"Appended {len(content)} bytes to {path}.")


def edit_file(args: dict[str, Any]) -> dict[str, Any]:
    path = str(args.get("path", "")).strip()
    old = str(args.get("old", ""))
    new = str(args.get("new", ""))
    if denied := _check_write(path):
        return denied
    if not old:
        return _error("edit_file needs the exact text to replace.")
    target = _resolve(path)
    if target is None or not target.is_file():
        return _error(f"No such file: {path}")
    try:
        body = target.read_text(encoding="utf-8")
    except OSError as exc:
        return _error(f"Could not read {path}: {exc}")
    count = body.count(old)
    if count == 0:
        return _error(f"That text does not appear in {path}.")
    if count > 1:
        return _error(f"That text appears {count} times in {path}; make it unique.")
    try:
        target.write_text(body.replace(old, new, 1), encoding="utf-8")
    except OSError as exc:
        return _error(f"Could not write {path}: {exc}")
    log.info("agent edited %s", path)
    return _text(f"Edited {path}.")


# --- specs ------------------------------------------------------------------


def _obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_STR = {"type": "string"}

SPECS: dict[str, dict[str, Any]] = {
    "read_file": {
        "description": (
            "Read a text file from the project, such as brain/goals.md or a past "
            "brief. Use this when the answer depends on what is written in a file "
            "rather than on the database."
        ),
        "parameters": _obj(
            {
                "path": {
                    **_STR,
                    "description": "Repo-relative path, e.g. brain/goals.md.",
                }
            },
            ["path"],
        ),
    },
    "list_files": {
        "description": (
            "List project files matching a glob, e.g. 'briefs/*.md'. Use this to "
            "find out what exists before reading."
        ),
        "parameters": _obj(
            {"pattern": {**_STR, "description": "Glob, e.g. briefs/*.md."}}, ["pattern"]
        ),
    },
    "search": {
        "description": (
            "Search file contents with a regular expression and return matching "
            "lines with their file and line number. Use this instead of reading "
            "every brief when looking for a mention of something."
        ),
        "parameters": _obj(
            {
                "pattern": {**_STR, "description": "Regular expression."},
                "glob": {**_STR, "description": "Files to search, default **/*.md."},
            },
            ["pattern"],
        ),
    },
    "write_file": {
        "description": (
            "Write a file, replacing it entirely. Only brain/ and briefs/ are "
            "writable. Use this for a new brief; prefer append_file for adding to "
            "an existing brain/ file."
        ),
        "parameters": _obj(
            {
                "path": {
                    **_STR,
                    "description": "Repo-relative path under brain/ or briefs/.",
                },
                "content": {**_STR, "description": "Full file contents."},
            },
            ["path", "content"],
        ),
    },
    "append_file": {
        "description": (
            "Add a line or short section to the end of an existing brain/ file "
            "without rewriting it. This is the preferred way to record something "
            "durable."
        ),
        "parameters": _obj(
            {
                "path": {**_STR, "description": "Repo-relative path under brain/."},
                "content": {**_STR, "description": "Text to append."},
            },
            ["path", "content"],
        ),
    },
    "edit_file": {
        "description": (
            "Replace one exact, unique piece of text in a file under brain/ or "
            "briefs/. Fails if the text is missing or appears more than once."
        ),
        "parameters": _obj(
            {
                "path": {
                    **_STR,
                    "description": "Repo-relative path under brain/ or briefs/.",
                },
                "old": {
                    **_STR,
                    "description": "Exact text to replace, must be unique.",
                },
                "new": {**_STR, "description": "Replacement text."},
            },
            ["path", "old", "new"],
        ),
    },
}

HANDLERS = {
    "read_file": read_file,
    "list_files": list_files,
    "search": search,
    "write_file": write_file,
    "append_file": append_file,
    "edit_file": edit_file,
}

READ_ONLY_NAMES = frozenset({"read_file", "list_files", "search"})
WRITE_NAMES = frozenset({"write_file", "append_file", "edit_file"})

assert set(SPECS) == set(HANDLERS), "fs tool specs and handlers disagree"
assert READ_ONLY_NAMES | WRITE_NAMES == set(HANDLERS), (
    "a tool is neither read nor write"
)


def specs(names: frozenset[str] | None = None) -> list[dict[str, Any]]:
    """OpenAI-format specs, optionally narrowed to a subset."""
    wanted = names if names is not None else set(SPECS)
    return [
        {"type": "function", "function": {"name": n, **SPECS[n]}}
        for n in SPECS
        if n in wanted
    ]


def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one filesystem tool. Never raises."""
    handler = HANDLERS.get(name)
    if handler is None:
        return _error(f"No such tool: {name}")
    try:
        return handler(args)
    except Exception as exc:
        log.exception("fs tool %s failed", name)
        return _error(f"{name} failed: {exc}")
