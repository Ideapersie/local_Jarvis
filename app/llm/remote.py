"""Claude, for the one path where a wrong answer costs money.

Portfolio analysis is the only thing billed after this migration. Everything
else runs on the local model for free, so the trade here is deliberate: pay for
Opus on the reasoning that decides what to do with real holdings, and pay for
nothing else.

Pricing lives here rather than in the callers. It used to be duplicated verbatim
in app/quick.py and app/services/triage.py, which meant a rate change needed two
edits and one of them would be missed.
"""

from __future__ import annotations

import logging
from typing import Any

import anthropic

from app import config
from app.llm.base import Completion, LLMError, Message, Usage

log = logging.getLogger("jarvis.llm.remote")

# USD per million tokens, input / output. Verified against the current model
# table 2026-09-01. Cache reads bill at roughly a tenth of input; that is
# applied below rather than modelled per row.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.00, 25.00),
    "claude-sonnet-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

CACHE_READ_DISCOUNT = 0.1

_client: anthropic.Anthropic | None = None


def client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        if not config.ANTHROPIC_API_KEY:
            raise LLMError("ANTHROPIC_API_KEY is not set; the portfolio tier needs it")
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


def reset_client() -> None:
    global _client
    _client = None


def price(model: str, usage: Usage) -> float:
    """Cost of one call. Unknown models cost 0 rather than raising.

    A missing price should show up as a suspiciously free row on the dashboard,
    not as an exception that loses the answer the user already paid for.
    """
    rates = PRICES.get(model)
    if rates is None:
        log.warning("no price for model %r; recording 0", model)
        return 0.0
    in_rate, out_rate = rates
    billable_input = (
        usage.input_tokens + usage.cache_read_input_tokens * CACHE_READ_DISCOUNT
    )
    return (billable_input * in_rate + usage.output_tokens * out_rate) / 1_000_000


class RemoteProvider:
    name = "remote"

    def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 16000,
        think: bool | None = None,
        model: str | None = None,
        effort: str = "high",
    ) -> Completion:
        model = model or config.MODEL_PORTFOLIO

        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": m.role, "content": m.content}
                for m in messages
                if m.role in ("user", "assistant")
            ],
            # Adaptive is the only on-mode on this family, and budget_tokens is
            # rejected outright. Sampling params are rejected too, so there is
            # deliberately no temperature here.
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": effort},
        }
        if system:
            body["system"] = system
        if schema is not None:
            body["output_config"] = {
                **body["output_config"],
                "format": {"type": "json_schema", "schema": schema},
            }
        if tools:
            body["tools"] = tools

        try:
            resp = client().messages.create(**body)
        except anthropic.APIError as exc:
            raise LLMError(f"{model} call failed: {exc}") from exc

        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        usage = Usage(
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
            cache_read_input_tokens=getattr(resp.usage, "cache_read_input_tokens", 0)
            or 0,
        )
        return Completion(
            text=text.strip(),
            usage=usage,
            cost_usd=price(model, usage),
            model=model,
            finish_reason=resp.stop_reason or "",
        )


provider = RemoteProvider()
