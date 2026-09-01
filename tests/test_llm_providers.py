"""Provider layer: wire formats, pricing, and the switches that cost money.

No network anywhere. The local server is not assumed to be running, and the
Anthropic client is never constructed against a real key - these test the
translation either side of the call, which is where the mistakes actually live.
"""

from __future__ import annotations

import json

import pytest

from app import config
from app.llm import local, remote
from app.llm.base import Completion, Message, ToolCall, Usage

# --- wire format ------------------------------------------------------------


def test_system_prompt_leads_the_message_list():
    wire = local._to_wire([Message("user", "hello")], "be terse")
    assert wire[0] == {"role": "system", "content": "be terse"}
    assert wire[1]["role"] == "user"


def test_no_system_entry_when_none_given():
    wire = local._to_wire([Message("user", "hello")], None)
    assert [m["role"] for m in wire] == ["user"]


def test_tool_result_carries_its_call_id():
    """A tool result without its id cannot be matched to the call it answers."""
    wire = local._to_wire(
        [Message("tool", "42 done", tool_call_id="call_7")],
        None,
    )
    assert wire[0] == {"role": "tool", "tool_call_id": "call_7", "content": "42 done"}


def test_assistant_tool_calls_are_serialised_as_json_strings():
    """The wire wants arguments as a JSON string, not a nested object."""
    msg = Message(
        "assistant",
        "",
        tool_calls=[ToolCall(id="c1", name="get_habits", arguments={"days": 14})],
    )
    wire = local._to_wire([msg], None)
    fn = wire[0]["tool_calls"][0]["function"]
    assert fn["name"] == "get_habits"
    assert json.loads(fn["arguments"]) == {"days": 14}


# --- tool call parsing ------------------------------------------------------


class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _Call:
    def __init__(self, name, arguments, id_="c0"):
        self.function = _Fn(name, arguments)
        self.id = id_


def test_unparseable_arguments_do_not_lose_the_call():
    """A malformed argument blob must still surface as a call.

    The loop reports the bad call back to the model, which can correct itself.
    Dropping it silently turns a recoverable turn into a mystery.
    """
    calls = local._parse_calls([_Call("get_habits", "{not json")])
    assert len(calls) == 1
    assert calls[0].name == "get_habits"
    assert calls[0].arguments == {}


def test_non_object_arguments_become_an_empty_dict():
    calls = local._parse_calls([_Call("get_habits", "[1, 2]")])
    assert calls[0].arguments == {}


def test_missing_id_is_backfilled():
    calls = local._parse_calls([_Call("get_tasks", "{}", id_="")])
    assert calls[0].id == "call_0"


# --- pricing ----------------------------------------------------------------


def test_opus_price_matches_the_published_rate():
    usage = Usage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert remote.price("claude-opus-5", usage) == pytest.approx(30.00)


def test_cache_reads_bill_at_a_discount():
    plain = remote.price("claude-opus-5", Usage(input_tokens=1_000_000))
    cached = remote.price(
        "claude-opus-5", Usage(input_tokens=0, cache_read_input_tokens=1_000_000)
    )
    assert cached == pytest.approx(plain * remote.CACHE_READ_DISCOUNT)


def test_unknown_model_costs_zero_rather_than_raising():
    """A missing price should look wrong on the dashboard, not lose the answer."""
    assert remote.price("some-new-model", Usage(input_tokens=1000)) == 0.0


def test_every_model_the_config_names_has_a_price():
    """Guards the case where a model is switched but the price table is not."""
    assert config.MODEL_PORTFOLIO in remote.PRICES


def test_portfolio_uses_opus():
    """The one billed path is deliberately the most capable model, not the cheapest."""
    assert config.MODEL_PORTFOLIO == "claude-opus-5"


# --- the thinking switch ----------------------------------------------------


def _body(monkeypatch, **kwargs):
    """Capture the request body without sending it."""
    captured = {}

    class _Completions:
        def create(self, **body):
            captured.update(body)
            raise RuntimeError("stop here, the body is what matters")

    class _Chat:
        completions = _Completions()

    class _Client:
        chat = _Chat()

    monkeypatch.setattr(local, "_client", _Client())
    with pytest.raises(RuntimeError):
        local.provider.complete([Message("user", "hi")], **kwargs)
    return captured


def test_thinking_is_disabled_by_default(monkeypatch):
    """Measured: reasoning costs 53 tokens and 10.9s against 4 and 0.8s."""
    body = _body(monkeypatch)
    assert body["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False


def test_thinking_can_be_asked_for(monkeypatch):
    body = _body(monkeypatch, think=True)
    assert "extra_body" not in body


def test_schema_is_sent_as_a_grammar_not_a_request(monkeypatch):
    """Unconstrained this model scored 0/6 on the quick tier schema."""
    schema = {"type": "object", "properties": {}}
    body = _body(monkeypatch, schema=schema)
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["schema"] is schema


def test_local_calls_are_free_but_still_counted():
    """Zero cost, real token counts: volume is what warns of a slow brief."""
    c = Completion(usage=Usage(input_tokens=100, output_tokens=20), cost_usd=0.0)
    assert c.cost_usd == 0.0
    assert c.usage.as_dict() == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_input_tokens": 0,
    }
