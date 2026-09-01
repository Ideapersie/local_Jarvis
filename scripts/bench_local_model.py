r"""Measure whether the local model is good enough to run Jarvis.

This is the gate for the whole local-LLM migration. Nothing downstream is worth
building until these numbers come back acceptable, because the failure mode of a
too-quantised model here is not a crash. It is a brief that quietly reports the
wrong streak, or a set_application_stage call with an invented stage.

Measures four things against the real schemas already in the repo, not toy ones:

  1. generation and prefill speed at realistic context
  2. structured-output validity against quick.RESPONSE_SCHEMA, with and without
     the grammar constraint, to prove constrained decoding is doing its job
  3. tool selection accuracy over replayed questions
  4. whether optional tool parameters are used when the question calls for them

Run llama-server first:

  scripts\start-llama.ps1          (models live in D:\LLM\models)

Usage:  python scripts/bench_local_model.py [--runs 10] [--skip speed]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from jsonschema import ValidationError, validate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import quick
from app.llm import tools as llm_tools

DEFAULT_BASE_URL = "http://127.0.0.1:8080/v1"
RESULT_PATH = Path(__file__).resolve().parent.parent / ".bench_result.json"

# Gate thresholds. Below any of these, the honest move is to change model or
# quant rather than to keep building on it.
MIN_TOOL_ACCURACY = 0.90
MIN_TOKENS_PER_SEC = 4.0
MIN_CONSTRAINED_JSON = 0.99


# --- test cases -------------------------------------------------------------

# (question, expected tool name, or None for "answer without calling a tool")
TOOL_CASES: list[tuple[str, str | None]] = [
    ("What's my current streak?", "get_habits"),
    ("Which habits have I kept up with?", "get_habits"),
    ("Did I do everything today?", "get_habits"),
    ("How has my gym habit looked over the last month?", "get_habit_history"),
    ("Show me the pattern for creatine over 30 days", "get_habit_history"),
    ("What's on my to-do list?", "get_tasks"),
    ("Anything outstanding I should do?", "get_tasks"),
    ("Show me completed tasks too", "get_tasks"),
    ("Where are my applications up to?", "get_applications"),
    ("Which applications are at the technical stage?", "get_applications"),
    ("What have I got left to prep for application 3?", "get_prep_notes"),
    ("Show me all prep notes for application 1, including resolved", "get_prep_notes"),
    ("How confident am I on my skills?", "get_skills"),
    ("Which skill is weakest?", "get_skills"),
    ("Move application 2 to the offer stage", "set_application_stage"),
    ("I got rejected from application 5", "set_application_stage"),
    ("The interview for application 4 is on 2026-09-15", "set_application_date"),
    ("Add a note on application 2 that I need to learn system design", "add_prep_note"),
    ("Mark prep note 7 as done", "resolve_prep_note"),
    ("Bump my Python confidence to 4", "set_skill_confidence"),
    ("What is the capital of France?", None),
]

# Questions whose correct answer needs an optional parameter that used to live
# only in the tool description prose. These are the ones a weak model drops.
OPTIONAL_PARAM_CASES: list[tuple[str, str, str]] = [
    ("How has my gym habit looked over the last 30 days?", "get_habit_history", "days"),
    ("Show me my completed tasks as well", "get_tasks", "include_done"),
    ("Which applications are at the technical stage?", "get_applications", "stage"),
    (
        "Show prep notes for application 1 including resolved",
        "get_prep_notes",
        "include_resolved",
    ),
]

JSON_QUESTIONS = [
    "What's my current streak?",
    "How many open tasks do I have?",
    "Which application is furthest along?",
    "How should I prepare for Thursday?",
    "What did I write in my goals file?",
]


# --- harness ----------------------------------------------------------------


@dataclass
class Result:
    name: str
    passed: int = 0
    total: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def rate(self) -> float:
        return self.passed / self.total if self.total else 0.0


class Client:
    def __init__(self, base_url: str, model: str, timeout: float) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.http = httpx.Client(timeout=timeout)

    def chat(self, **body: Any) -> dict[str, Any]:
        body.setdefault("model", self.model)
        # Greedy decoding: the bench measures capability, and sampling noise
        # would make a rerun disagree with itself.
        body.setdefault("temperature", 0.0)
        r = self.http.post(f"{self.base_url}/chat/completions", json=body)
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self.http.close()


def _timings(resp: dict[str, Any]) -> tuple[float, float] | None:
    """(prefill tok/s, generation tok/s) from llama.cpp's timings extension."""
    t = resp.get("timings")
    if not isinstance(t, dict):
        return None
    try:
        prefill = t["prompt_n"] / (t["prompt_ms"] / 1000.0)
        generate = t["predicted_n"] / (t["predicted_ms"] / 1000.0)
    except (KeyError, ZeroDivisionError, TypeError):
        return None
    return prefill, generate


def _first_call(resp: dict[str, Any]) -> dict[str, Any] | None:
    calls = resp["choices"][0]["message"].get("tool_calls") or []
    return calls[0] if calls else None


# --- the four measurements --------------------------------------------------


def bench_speed(c: Client, snapshot: str) -> Result:
    res = Result("speed", total=1)

    # Pad towards 16k tokens so prefill is measured under real load rather than
    # on a toy prompt that fits in a single batch.
    repeats = max(1, 60000 // max(len(snapshot), 1))
    filler = (snapshot + "\n\n") * repeats

    started = time.monotonic()
    resp = c.chat(
        messages=[
            {"role": "system", "content": quick.SYSTEM},
            {
                "role": "user",
                "content": (
                    f"<snapshot>\n{filler}\n</snapshot>\n\n"
                    "Summarise my habits in two sentences."
                ),
            },
        ],
        max_tokens=256,
    )
    wall = time.monotonic() - started

    timings = _timings(resp)
    if timings:
        prefill, generate = timings
        res.notes.append(f"generate {generate:7.1f} tok/s")
        res.notes.append(f"prefill  {prefill:7.1f} tok/s")
    else:
        usage = resp.get("usage", {})
        generate = usage.get("completion_tokens", 0) / wall if wall else 0.0
        res.notes.append(f"generate {generate:7.1f} tok/s (wall clock)")
        res.notes.append(f"prompt tokens {usage.get('prompt_tokens', '?')}")

    res.notes.append(f"wall {wall:.1f}s")
    res.notes.append(f"gate is >= {MIN_TOKENS_PER_SEC} tok/s")
    res.passed = 1 if generate >= MIN_TOKENS_PER_SEC else 0
    return res


def bench_json(c: Client, snapshot: str, constrained: bool, runs: int) -> Result:
    res = Result("json_constrained" if constrained else "json_freeform")

    extra: dict[str, Any] = {}
    if constrained:
        extra["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "quick_response", "schema": quick.RESPONSE_SCHEMA},
        }

    for i in range(runs):
        question = JSON_QUESTIONS[i % len(JSON_QUESTIONS)]
        res.total += 1
        try:
            resp = c.chat(
                messages=[
                    {"role": "system", "content": quick.SYSTEM},
                    {
                        "role": "user",
                        "content": f"<snapshot>\n{snapshot}\n</snapshot>\n\n{question}",
                    },
                ],
                max_tokens=1024,
                **extra,
            )
            text = resp["choices"][0]["message"]["content"]
            validate(instance=json.loads(text), schema=quick.RESPONSE_SCHEMA)
            res.passed += 1
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            res.notes.append(f"{question[:36]!r}: {type(exc).__name__}")
        except (httpx.HTTPError, KeyError, IndexError) as exc:
            res.notes.append(f"{question[:36]!r}: transport {exc}")
    return res


def bench_tools(c: Client, snapshot: str) -> Result:
    res = Result("tool_selection")
    specs = llm_tools.specs()

    for question, expected in TOOL_CASES:
        res.total += 1
        try:
            resp = c.chat(
                messages=[
                    {"role": "system", "content": quick.SYSTEM},
                    {
                        "role": "user",
                        "content": f"<snapshot>\n{snapshot}\n</snapshot>\n\n{question}",
                    },
                ],
                tools=specs,
                max_tokens=1024,
            )
            call = _first_call(resp)
            got = call["function"]["name"] if call else None

            if got != expected:
                res.notes.append(f"{question[:36]!r}: wanted {expected}, got {got}")
                continue

            # A correct tool name carrying unparseable arguments is still a
            # failure: the handler receives nothing it can use.
            if call:
                args = json.loads(call["function"]["arguments"] or "{}")
                validate(instance=args, schema=llm_tools.SCHEMAS[got])
            res.passed += 1
        except (json.JSONDecodeError, ValidationError) as exc:
            res.notes.append(f"{question[:36]!r}: bad args, {type(exc).__name__}")
        except (httpx.HTTPError, KeyError, IndexError) as exc:
            res.notes.append(f"{question[:36]!r}: transport {exc}")
    return res


def bench_optional_params(c: Client, snapshot: str) -> Result:
    res = Result("optional_params")
    specs = llm_tools.specs()

    for question, tool_name, param in OPTIONAL_PARAM_CASES:
        res.total += 1
        try:
            resp = c.chat(
                messages=[
                    {"role": "system", "content": quick.SYSTEM},
                    {
                        "role": "user",
                        "content": f"<snapshot>\n{snapshot}\n</snapshot>\n\n{question}",
                    },
                ],
                tools=specs,
                max_tokens=1024,
            )
            call = _first_call(resp)
            got = call["function"]["name"] if call else None
            if got != tool_name:
                res.notes.append(f"{param}: wrong tool, wanted {tool_name}, got {got}")
                continue

            args = json.loads(call["function"]["arguments"] or "{}")
            if param in args:
                res.passed += 1
            else:
                res.notes.append(f"{param}: omitted, model sent {sorted(args)}")
        except (json.JSONDecodeError, httpx.HTTPError, KeyError, IndexError) as exc:
            res.notes.append(f"{param}: {type(exc).__name__} {exc}")
    return res


# --- reporting --------------------------------------------------------------


def report(results: list[Result]) -> bool:
    print()
    print("=" * 72)
    print(f"{'measurement':<20} {'result':>14}   notes")
    print("-" * 72)

    for r in results:
        if r.name == "speed":
            headline = "PASS" if r.passed else "FAIL"
        else:
            headline = f"{r.passed}/{r.total} {r.rate:>6.0%}"
        first = r.notes[0] if r.notes else "all correct"
        print(f"{r.name:<20} {headline:>14}   {first}")
        for note in r.notes[1:6]:
            print(f"{'':<20} {'':>14}   {note}")
        if len(r.notes) > 6:
            print(f"{'':<20} {'':>14}   ... and {len(r.notes) - 6} more")
    print("=" * 72)

    by_name = {r.name: r for r in results}
    ok = True
    failures: list[str] = []

    speed = by_name.get("speed")
    if speed and not speed.passed:
        print(f"GATE FAIL: generation below {MIN_TOKENS_PER_SEC} tok/s.")
        failures.append("speed")
        ok = False

    selection = by_name.get("tool_selection")
    if selection and selection.rate < MIN_TOOL_ACCURACY:
        print(
            f"GATE FAIL: tool selection {selection.rate:.0%}, "
            f"need {MIN_TOOL_ACCURACY:.0%}."
        )
        failures.append(f"tool selection {selection.rate:.0%}")
        ok = False

    constrained = by_name.get("json_constrained")
    if constrained and constrained.rate < MIN_CONSTRAINED_JSON:
        print(
            f"GATE FAIL: constrained JSON {constrained.rate:.0%}. "
            "Grammar is not being applied."
        )
        failures.append(f"constrained json {constrained.rate:.0%}")
        ok = False

    freeform = by_name.get("json_freeform")
    if freeform and constrained:
        print(
            f"Grammar constraint moved JSON validity "
            f"{freeform.rate:.0%} -> {constrained.rate:.0%}."
        )

    params = by_name.get("optional_params")
    if params and params.rate < 1.0:
        # Not a gate. It degrades answers rather than corrupting rows, and the
        # handlers all have sane defaults.
        print(
            f"Note: optional params used {params.rate:.0%} of the time. "
            "Answers will be less precise."
        )

    print(
        "GATE PASS: safe to build on this model."
        if ok
        else "Do not proceed. Change quant or model, or add RAM."
    )

    # Persisted so scripts/migration_status.py can report the gate without
    # re-running a bench that takes minutes and loads the GPU.
    RESULT_PATH.write_text(
        json.dumps(
            {
                "when": datetime.now().isoformat(timespec="seconds"),
                "gate_pass": ok,
                "failures": failures,
                "measurements": {
                    r.name: {"passed": r.passed, "total": r.total, "notes": r.notes}
                    for r in results
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"Wrote {RESULT_PATH.name}")
    return ok


def main() -> int:
    p = argparse.ArgumentParser(
        description="Bench the local model against Jarvis's real schemas."
    )
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument(
        "--model",
        default="local",
        help="llama-server ignores this; sent for OpenAI compatibility",
    )
    p.add_argument("--runs", type=int, default=10, help="iterations for each JSON test")
    p.add_argument("--timeout", type=float, default=600.0)
    p.add_argument(
        "--skip", nargs="*", default=[], choices=["speed", "json", "tools", "params"]
    )
    args = p.parse_args()

    c = Client(args.base_url, args.model, args.timeout)
    health = c.base_url.rsplit("/v1", 1)[0] + "/health"
    try:
        c.http.get(health)
    except httpx.HTTPError as exc:
        print(f"Cannot reach llama-server at {args.base_url}: {exc}")
        print("Start it first. See this file's docstring for the command.")
        c.close()
        return 2

    snapshot = quick.snapshot()
    print(f"snapshot is {len(snapshot)} chars")

    results: list[Result] = []
    try:
        if "speed" not in args.skip:
            results.append(bench_speed(c, snapshot))
        if "json" not in args.skip:
            results.append(bench_json(c, snapshot, constrained=False, runs=args.runs))
            results.append(bench_json(c, snapshot, constrained=True, runs=args.runs))
        if "tools" not in args.skip:
            results.append(bench_tools(c, snapshot))
        if "params" not in args.skip:
            results.append(bench_optional_params(c, snapshot))
    finally:
        c.close()

    return 0 if report(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
