"""The local model, served by llama.cpp over its OpenAI-compatible endpoint.

Three things here are not obvious, and all three were measured rather than
assumed (scripts/bench_local_model.py, scripts/sweep_ngl.py):

Reasoning is off by default. Qwen3.8 thinks before answering unless told not to,
and on a database lookup that thinking is pure cost: 53 tokens and 10.9s with it
on against 4 tokens and 0.8s with it off, for the same correct answer.
Throughput is identical either way - it is the token count that collapses. Pass
think=True for genuinely multi-step work like the brief.

A schema is enforced, never requested. Unconstrained, this model scored 0/6 on
quick.RESPONSE_SCHEMA: it answers correctly and then ignores the format, writing
markdown with "escalate=false" on the end. Constrained it is 6/6. At this quant
the model cannot self-impose a format from prose, so `schema` goes to the server
as a grammar rather than into the prompt as a request.

Cost is always zero, and calls are still recorded. Token volume is what warns
you that a prompt has grown enough to make the morning brief slow.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import openai

from app import config
from app.llm.base import Completion, LLMError, Message, ToolCall, Usage

log = logging.getLogger("jarvis.llm.local")

_client: openai.OpenAI | None = None


def client() -> openai.OpenAI:
    global _client
    if _client is None:
        _client = openai.OpenAI(
            base_url=config.LOCAL_BASE_URL,
            # llama-server ignores the key but the SDK requires one.
            api_key="not-needed",
            timeout=config.LOCAL_TIMEOUT_S,
            max_retries=1,
        )
    return _client


def reset_client() -> None:
    """Drop the cached client. For tests, and after a base-url change."""
    global _client
    _client = None


def _to_wire(messages: list[Message], system: str | None) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        if m.role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.tool_call_id or "",
                    "content": m.content,
                }
            )
            continue
        entry: dict[str, Any] = {"role": m.role, "content": m.content}
        if m.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.name,
                        "arguments": json.dumps(tc.arguments),
                    },
                }
                for tc in m.tool_calls
            ]
        out.append(entry)
    return out


def _parse_calls(raw: Any) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for i, tc in enumerate(raw or []):
        try:
            args = json.loads(tc.function.arguments or "{}")
        except json.JSONDecodeError:
            # Keep the turn alive: the loop reports the bad call back to the
            # model, which is more useful than dropping it silently.
            log.warning("tool call %s had unparseable arguments", tc.function.name)
            args = {}
        if not isinstance(args, dict):
            args = {}
        calls.append(
            ToolCall(
                id=getattr(tc, "id", "") or f"call_{i}",
                name=tc.function.name,
                arguments=args,
            )
        )
    return calls


class LocalProvider:
    name = "local"

    def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
        think: bool | None = None,
    ) -> Completion:
        body: dict[str, Any] = {
            "model": config.LOCAL_MODEL,
            "messages": _to_wire(messages, system),
            "max_tokens": max_tokens,
            "temperature": config.LOCAL_TEMPERATURE,
        }
        if tools:
            body["tools"] = tools
        if schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": schema},
            }
        if think is None:
            think = config.LOCAL_THINK_DEFAULT
        if not think:
            # llama.cpp extensions are not in the OpenAI schema, and the SDK
            # rejects unknown top-level kwargs outright. extra_body is the
            # supported way through; passing it directly raises TypeError.
            body["extra_body"] = {"chat_template_kwargs": {"enable_thinking": False}}

        try:
            resp = client().chat.completions.create(**body)
        except openai.OpenAIError as exc:
            raise LLMError(f"local model call failed: {exc}") from exc

        choice = resp.choices[0]
        msg = choice.message
        u = resp.usage

        return Completion(
            text=(msg.content or "").strip(),
            reasoning=(getattr(msg, "reasoning_content", None) or "").strip(),
            tool_calls=_parse_calls(getattr(msg, "tool_calls", None)),
            usage=Usage(
                input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                output_tokens=getattr(u, "completion_tokens", 0) or 0,
            ),
            cost_usd=0.0,
            model=config.LOCAL_MODEL,
            finish_reason=choice.finish_reason or "",
        )


provider = LocalProvider()


def available() -> bool:
    """Whether llama-server is up. Callers degrade rather than crash."""
    try:
        import httpx

        base = config.LOCAL_BASE_URL.rsplit("/v1", 1)[0]
        return httpx.get(f"{base}/health", timeout=2.0).status_code == 200
    except Exception:
        return False
