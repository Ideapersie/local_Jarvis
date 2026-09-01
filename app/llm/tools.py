"""Tool specs for the local model, in OpenAI function-calling format.

The eleven handlers in app/agent_tools.py are plain async callables and are
reused untouched. Only the transport changes: claude-agent-sdk wrapped them in
an in-process MCP server, llama-server wants OpenAI-shaped JSON Schema.

Why the schemas are written out here rather than read off SdkMcpTool.input_schema:
that field holds the python-type shorthand the @tool decorator was given, e.g.
{"name": str}. It cannot express required, enums, or defaults, and six handlers
read parameters that never appear in it at all - get_tasks reads include_done,
get_applications reads stage, and so on. Sonnet inferred those from the
description prose. A 3-bit local model will not, and an unconstrained "stage"
string means a silently corrupted application row. Declaring them properly is
the single cheapest accuracy win available in this migration.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.agent_tools import ALL_TOOLS, PREP_KINDS, STAGES

log = logging.getLogger("jarvis.llm.tools")


def _obj(
    properties: dict[str, Any] | None = None, required: list[str] | None = None
) -> dict[str, Any]:
    """A JSON Schema object with additionalProperties pinned off.

    Constrained decoding needs a closed schema: left open, the grammar permits
    invented keys and the handler silently ignores them.
    """
    return {
        "type": "object",
        "properties": properties or {},
        "required": required or [],
        "additionalProperties": False,
    }


# Full JSON Schema per tool. Keys must match SdkMcpTool.name exactly; the
# completeness check at import time enforces that.
SCHEMAS: dict[str, dict[str, Any]] = {
    "get_habits": _obj(),
    "get_habit_history": _obj(
        {
            "name": {
                "type": "string",
                "description": "Habit name, exactly as get_habits reports it.",
            },
            "days": {
                "type": "integer",
                "minimum": 1,
                "maximum": 365,
                "default": 14,
                "description": "How many days back to look. Defaults to 14.",
            },
        },
        required=["name"],
    ),
    "get_tasks": _obj(
        {
            "include_done": {
                "type": "boolean",
                "default": False,
                "description": "Include completed tasks. Defaults to false.",
            }
        }
    ),
    "get_applications": _obj(
        {
            "stage": {
                "type": "string",
                "enum": STAGES,
                "description": "Filter to one stage. Omit for all applications.",
            }
        }
    ),
    "get_prep_notes": _obj(
        {
            "application_id": {"type": "integer", "description": "Application id."},
            "include_resolved": {
                "type": "boolean",
                "default": False,
                "description": "Include notes already resolved. Defaults to false.",
            },
        },
        required=["application_id"],
    ),
    "get_skills": _obj(),
    "set_application_stage": _obj(
        {
            "application_id": {"type": "integer", "description": "Application id."},
            "stage": {"type": "string", "enum": STAGES, "description": "New stage."},
        },
        required=["application_id", "stage"],
    ),
    "set_application_date": _obj(
        {
            "application_id": {"type": "integer", "description": "Application id."},
            "date": {
                "type": "string",
                "description": "ISO date YYYY-MM-DD, or an empty string to clear it.",
            },
        },
        required=["application_id", "date"],
    ),
    "add_prep_note": _obj(
        {
            "application_id": {"type": "integer", "description": "Application id."},
            "kind": {"type": "string", "enum": PREP_KINDS, "description": "Note kind."},
            "body": {"type": "string", "description": "The note itself."},
        },
        required=["application_id", "kind", "body"],
    ),
    "resolve_prep_note": _obj(
        {
            "note_id": {"type": "integer", "description": "Prep note id."},
            "resolved": {
                "type": "boolean",
                "default": True,
                "description": "Set false to reopen a resolved note. Defaults to true.",
            },
        },
        required=["note_id"],
    ),
    "set_skill_confidence": _obj(
        {
            "name": {"type": "string", "description": "Skill name."},
            "confidence": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "description": "Current confidence, 1 to 5.",
            },
            "target": {
                "type": "integer",
                "minimum": 1,
                "maximum": 5,
                "default": 3,
                "description": "Target confidence, 1 to 5. Defaults to 3.",
            },
        },
        required=["name", "confidence"],
    ),
}


def _check_complete() -> None:
    """Fail at import if SCHEMAS and ALL_TOOLS have drifted apart.

    A tool added to agent_tools.py without a schema here would otherwise be
    invisible to the model, which looks like the model being stupid rather than
    the tool being absent.
    """
    declared = {t.name for t in ALL_TOOLS}
    written = set(SCHEMAS)
    if missing := declared - written:
        raise RuntimeError(
            f"tools with no JSON Schema in app/llm/tools.py: {sorted(missing)}"
        )
    if extra := written - declared:
        raise RuntimeError(
            f"schemas with no matching tool in agent_tools.py: {sorted(extra)}"
        )


_check_complete()


def specs() -> list[dict[str, Any]]:
    """Every jarvis tool as an OpenAI-format function spec."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": SCHEMAS[t.name],
            },
        }
        for t in ALL_TOOLS
    ]


HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    t.name: t.handler for t in ALL_TOOLS
}

# Read-only tools can be auto-approved and retried freely; the career writers
# touch real rows and are gated by scope in app/loop.py.
READ_ONLY_NAMES = frozenset(
    t.name for t in ALL_TOOLS if getattr(t.annotations, "readOnlyHint", False)
)


async def dispatch(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Run one tool call. Never raises: the loop needs a result block either way."""
    handler = HANDLERS.get(name)
    if handler is None:
        log.warning("model called unknown tool: %s", name)
        return {
            "content": [{"type": "text", "text": f"No such tool: {name}"}],
            "is_error": True,
        }
    try:
        return await handler(args)
    except Exception as exc:
        # A tool that raises would otherwise kill the turn. Hand the model the
        # error so it can correct itself or say it could not find out.
        log.exception("tool %s failed", name)
        return {
            "content": [{"type": "text", "text": f"{name} failed: {exc}"}],
            "is_error": True,
        }
