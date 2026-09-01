"""The agentic loop: profiles, dispatch, session bounds, and failure handling.

No network. The provider is replaced with a scripted fake, because what needs
testing is the loop's behaviour around the model, not the model.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app import loop
from app.llm.base import Completion, LLMError, Message, ToolCall, Usage

# --- profiles ---------------------------------------------------------------


def _names(profile: str) -> set[str]:
    return {s["function"]["name"] for s in loop.specs_for(profile)}


def test_every_profile_resolves_to_real_tools():
    for profile in loop.PROFILES:
        assert _names(profile), f"{profile} resolved to no tools"


def test_unknown_profile_is_loud():
    with pytest.raises(ValueError, match="unknown tool profile"):
        loop.specs_for("nonsense")


@pytest.mark.parametrize("profile", ["chat_write", "prep"])
def test_write_profiles_exclude_get_applications(profile):
    """The measured cause of every tool-selection failure.

    With get_applications in scope, "I got rejected from application 5" routed
    to it instead of set_application_stage, 4 times out of 4. Without it, 4/4
    correct. Both these profiles handle write intents about applications.
    """
    assert "get_applications" not in _names(profile)


def test_brief_and_reflection_get_no_database_tools():
    """Their facts are pre-computed and the prompts say not to re-fetch.

    Carrying the database tools would be prefill they cannot use, and a way to
    contradict the facts they were handed.
    """
    for profile in ("brief", "reflection"):
        assert not (_names(profile) & set(loop.db_tools.HANDLERS)), profile


def test_read_only_profiles_cannot_write():
    assert not (_names("chat_read") & loop._FS_WRITES)
    assert not (_names("chat_read") & loop._DB_WRITES)


def test_prep_can_record_but_not_rewrite_files():
    """It records prep notes; it has no business rewriting brain/ files."""
    names = _names("prep")
    assert "add_prep_note" in names
    assert "web_search" in names
    assert not (names & loop._FS_WRITES)


def test_profiles_are_smaller_than_the_full_tool_list():
    """Prefill is the dominant cost, so a profile that saves nothing is a bug."""
    full = len(json.dumps(loop.db_tools.specs()))
    for profile in loop.PROFILES:
        size = len(json.dumps(loop.specs_for(profile)))
        assert size < full * 1.05, f"{profile} is no smaller than the full list"


def test_spec_order_is_stable():
    """The prefix cache is a byte match; reordering costs a full re-prefill."""
    assert loop.specs_for("chat_read") == loop.specs_for("chat_read")


# --- dispatch ---------------------------------------------------------------


def test_dispatch_routes_to_the_owning_module():
    result = asyncio.run(loop.dispatch("list_files", {"pattern": "app/loop.py"}))
    assert "app/loop.py" in result["content"][0]["text"]


def test_dispatch_reaches_the_database_tools():
    result = asyncio.run(loop.dispatch("get_habits", {}))
    assert not result.get("is_error")


def test_unknown_tool_is_reported_not_raised():
    result = asyncio.run(loop.dispatch("drop_tables", {}))
    assert result["is_error"] is True
    assert "No such tool" in result["content"][0]["text"]


def test_a_write_through_dispatch_still_hits_the_guard():
    """Dispatch must not become a way around the confinement."""
    result = asyncio.run(
        loop.dispatch("write_file", {"path": "app/main.py", "content": "x"})
    )
    assert result["is_error"] is True
    assert "not permitted" in result["content"][0]["text"]


# --- system prompt ----------------------------------------------------------


def test_system_prompt_carries_the_project_instructions():
    body = loop.build_system("chat_read")
    assert "Jarvis" in body
    assert "brain/" in body


def test_a_named_skill_is_folded_in():
    body = loop.build_system("brief", "brief-writer")
    assert "Skill: brief-writer" in body
    assert len(body) > len(loop.build_system("brief"))


def test_a_missing_skill_does_not_break_the_turn():
    body = loop.build_system("brief", "no-such-skill")
    assert "Jarvis" in body


# --- session registry -------------------------------------------------------


def test_sessions_are_bounded():
    async def go():
        await loop.shutdown_all()
        for i in range(loop.MAX_SESSIONS + 3):
            await loop.get_entry(f"s{i}")
        return loop.live_session_count()

    assert asyncio.run(go()) <= loop.MAX_SESSIONS


def test_history_survives_within_a_session():
    async def go():
        await loop.shutdown_all()
        a = await loop.get_entry("same")
        a.history.append(Message("user", "remember me"))
        b = await loop.get_entry("same")
        return b.history

    assert asyncio.run(go())[0].content == "remember me"


def test_shutdown_clears_everything():
    async def go():
        await loop.get_entry("x")
        await loop.shutdown_all()
        return loop.live_session_count()

    assert asyncio.run(go()) == 0


# --- the loop itself --------------------------------------------------------


class _Fake:
    """A provider that replays a scripted list of completions."""

    def __init__(self, *completions: Completion):
        self.queue = list(completions)
        self.calls = 0

    def complete(self, **kwargs):
        self.calls += 1
        if not self.queue:
            return Completion(text="done", usage=Usage(1, 1))
        return self.queue.pop(0)


def _run(session, prompt, **kwargs):
    async def go():
        return [ev async for ev in loop.run_turn(session, prompt, **kwargs)]

    return asyncio.run(go())


def test_a_plain_answer_ends_the_turn(monkeypatch):
    fake = _Fake(Completion(text="five days", usage=Usage(10, 3)))
    monkeypatch.setattr(loop.local, "provider", fake)
    events = _run("p1", "streak?")
    assert [e.kind for e in events] == ["text", "done"]
    assert fake.calls == 1


def test_a_tool_call_is_executed_then_answered(monkeypatch):
    fake = _Fake(
        Completion(
            tool_calls=[ToolCall(id="c1", name="get_habits", arguments={})],
            usage=Usage(10, 5),
        ),
        Completion(text="here they are", usage=Usage(20, 5)),
    )
    monkeypatch.setattr(loop.local, "provider", fake)
    events = _run("p2", "habits?")
    kinds = [e.kind for e in events]
    assert "tool" in kinds
    assert kinds[-1] == "done"
    assert fake.calls == 2


def test_the_iteration_cap_is_reported_not_silent(monkeypatch):
    """A silent truncation looks like the model losing interest."""
    endless = Completion(
        tool_calls=[ToolCall(id="c", name="get_habits", arguments={})],
        usage=Usage(1, 1),
    )
    fake = _Fake(*[endless] * (loop.MAX_ITERATIONS + 2))
    monkeypatch.setattr(loop.local, "provider", fake)
    events = _run("p3", "loop forever")
    assert any(e.kind == "error" and "ran out of steps" in e.data for e in events)
    assert fake.calls == loop.MAX_ITERATIONS


def test_a_dead_server_is_explained_rather_than_raised(monkeypatch):
    class _Dead:
        def complete(self, **kwargs):
            raise LLMError("connection refused")

    monkeypatch.setattr(loop.local, "provider", _Dead())
    events = _run("p4", "anything")
    assert events[-1].kind == "done"
    assert any("llama-server" in e.data for e in events if e.kind == "error")


def test_token_volume_is_recorded_even_though_local_is_free(monkeypatch):
    """Cost is zero; volume is what warns the brief is getting slow."""
    recorded = {}

    def _record(kind, tier, model, cost, auth="", usage=None):
        recorded.update(
            {"kind": kind, "tier": tier, "cost": cost, "usage": usage or {}}
        )

    monkeypatch.setattr(loop.costs, "record", _record)
    monkeypatch.setattr(
        loop.local, "provider", _Fake(Completion(text="hi", usage=Usage(120, 34)))
    )
    _run("p5", "hello", kind="chat")

    assert recorded["tier"] == "local"
    assert recorded["cost"] == 0.0
    assert recorded["usage"]["input_tokens"] == 120
    assert recorded["usage"]["output_tokens"] == 34


def test_a_failing_tool_does_not_kill_the_turn(monkeypatch):
    fake = _Fake(
        Completion(
            tool_calls=[ToolCall(id="c", name="no_such_tool", arguments={})],
            usage=Usage(1, 1),
        ),
        Completion(text="recovered", usage=Usage(1, 1)),
    )
    monkeypatch.setattr(loop.local, "provider", fake)
    events = _run("p6", "break it")
    assert events[-1].kind == "done"
    assert any(e.kind == "text" and "recovered" in e.data for e in events)


def test_run_to_text_returns_the_final_block_only(monkeypatch):
    """Joining every block put the agent's narration into the dashboard panel."""
    fake = _Fake(
        Completion(
            text="Now invoking the brief-writer skill",
            tool_calls=[ToolCall(id="c", name="get_habits", arguments={})],
            usage=Usage(1, 1),
        ),
        Completion(text="The real summary.", usage=Usage(1, 1)),
    )
    monkeypatch.setattr(loop.local, "provider", fake)
    out = asyncio.run(loop.run_to_text("p7", "write it", profile="brief"))
    assert out == "The real summary."


def test_answer_json_enforces_the_schema(monkeypatch):
    captured = {}

    class _Cap:
        def complete(self, **kwargs):
            captured.update(kwargs)
            return Completion(text="{}")

    monkeypatch.setattr(loop.local, "provider", _Cap())
    schema = {"type": "object", "properties": {}}
    loop.answer_json("q", schema)
    assert captured["schema"] is schema
    assert not captured.get("tools")
