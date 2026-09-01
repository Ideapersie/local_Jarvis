"""Shared shapes for both model providers.

The local model and Claude return different objects, and the callers should not
care which one answered. Everything here is deliberately small: enough to carry
a turn and its cost, nothing that assumes a particular provider's wire format.

Cost is carried on the result rather than looked up later because only the
provider knows its own pricing, and a local call genuinely costs zero. Recording
zero-cost calls is still worth doing - token volume is the thing that tells you
whether the 06:30 brief is about to take ten minutes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

Role = Literal["system", "user", "assistant", "tool"]


class LLMError(RuntimeError):
    """A provider call failed. Callers decide whether to degrade or give up."""


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class Message:
    role: Role
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    # Set on a tool result, naming the call it answers.
    tool_call_id: str | None = None


@dataclass(slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0

    def as_dict(self) -> dict[str, int]:
        """The shape services.costs.record expects."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
        }


@dataclass(slots=True)
class Completion:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0
    model: str = ""
    finish_reason: str = ""
    # Kept separate from text. Qwen3.8 reasons by default and llama-server
    # returns that in its own field; folding it into text would put the model's
    # working out into the brief.
    reasoning: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class Provider(Protocol):
    """What app/loop.py needs from a model, and nothing more."""

    name: str

    def complete(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
        tools: list[dict[str, Any]] | None = None,
        schema: dict[str, Any] | None = None,
        max_tokens: int = 1024,
        think: bool | None = None,
    ) -> Completion: ...
