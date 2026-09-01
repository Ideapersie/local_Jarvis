r"""Tool-selection accuracy, measured in resumable chunks.

Same cases as bench_local_model.py, but each result is appended to a JSONL file
as soon as it lands, and already-recorded cases are skipped on the next run. A
full pass is 25 cases at roughly 20-30s each, which is long enough that a single
foreground run gets killed part-way; without incremental persistence every kill
threw away all the work.

Run it repeatedly until it reports complete, then read the summary:

  python scripts/bench_tools_chunk.py --count 6      # a few at a time
  python scripts/bench_tools_chunk.py --summary      # aggregate, no requests
  python scripts/bench_tools_chunk.py --reset        # start over
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from jsonschema import ValidationError, validate

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bench_local_model import (
    MIN_TOOL_ACCURACY,
    OPTIONAL_PARAM_CASES,
    TOOL_CASES,
    Client,
    agent_system,
    server_ctx,
)

from app.llm import tools as llm_tools

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / ".bench_tools.jsonl"
BASE_URL = "http://127.0.0.1:8080/v1"


def load() -> dict[str, dict[str, Any]]:
    if not STATE.exists():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in STATE.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rec = json.loads(line)
            out[rec["question"]] = rec
    return out


def append(rec: dict[str, Any]) -> None:
    with STATE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


def all_cases() -> list[tuple[str, str | None, str | None]]:
    """(question, expected tool, required optional param or None)."""
    cases: list[tuple[str, str | None, str | None]] = [
        (q, tool, None) for q, tool in TOOL_CASES
    ]
    cases += [(q, tool, param) for q, tool, param in OPTIONAL_PARAM_CASES]
    return cases


def run_one(
    c: Client, snapshot: str, question: str, expected: str | None, param: str | None
) -> dict[str, Any]:
    started = time.monotonic()
    rec: dict[str, Any] = {
        "question": question,
        "expected": expected,
        "param": param,
        "got": None,
        "args": None,
        "ok": False,
        "note": "",
    }
    try:
        resp = c.chat(
            messages=[
                {"role": "system", "content": agent_system()},
                {
                    "role": "user",
                    "content": f"<snapshot>\n{snapshot}\n</snapshot>\n\n{question}",
                },
            ],
            tools=llm_tools.specs(),
            max_tokens=400,
        )
        calls = resp["choices"][0]["message"].get("tool_calls") or []
        call = calls[0] if calls else None
        got = call["function"]["name"] if call else None
        rec["got"] = got

        if got != expected:
            rec["note"] = f"wanted {expected}, got {got}"
        elif call:
            args = json.loads(call["function"]["arguments"] or "{}")
            rec["args"] = args
            try:
                validate(instance=args, schema=llm_tools.SCHEMAS[got])
            except ValidationError as exc:
                rec["note"] = f"args fail schema: {exc.message[:80]}"
            else:
                if param and param not in args:
                    rec["note"] = f"omitted optional {param!r}"
                else:
                    rec["ok"] = True
        else:
            rec["ok"] = True  # correctly declined to call a tool
    except (httpx.HTTPError, KeyError, IndexError, json.JSONDecodeError) as exc:
        rec["note"] = f"{type(exc).__name__}: {exc}"

    rec["seconds"] = round(time.monotonic() - started, 1)
    return rec


def summarise(done: dict[str, dict[str, Any]]) -> None:
    cases = all_cases()
    recorded = [done[q] for q, _, _ in cases if q in done]
    if not recorded:
        print("nothing recorded yet")
        return

    passed = sum(1 for r in recorded if r["ok"])
    rate = passed / len(recorded)
    print()
    print("=" * 72)
    print(
        f"tool selection  {passed}/{len(recorded)}  {rate:.0%}   "
        f"({len(cases) - len(recorded)} cases not yet run)"
    )
    print("-" * 72)
    for r in recorded:
        if not r["ok"]:
            print(f"  FAIL  {r['question'][:44]!r}")
            print(f"        {r['note']}")
    if all(r["ok"] for r in recorded):
        print("  every recorded case correct")
    print("=" * 72)

    if len(recorded) == len(cases):
        verdict = "PASS" if rate >= MIN_TOOL_ACCURACY else "FAIL"
        print(f"COMPLETE - gate {verdict} (need {MIN_TOOL_ACCURACY:.0%})")
    else:
        print(f"INCOMPLETE - rerun to continue ({len(recorded)}/{len(cases)} done)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--count", type=int, default=6, help="cases to run this pass")
    ap.add_argument("--base-url", default=BASE_URL)
    ap.add_argument("--summary", action="store_true", help="aggregate only")
    ap.add_argument("--reset", action="store_true", help="discard recorded results")
    ap.add_argument("--think", action="store_true")
    args = ap.parse_args()

    if args.reset:
        STATE.unlink(missing_ok=True)
        print("state cleared")
        return 0

    done = load()
    if args.summary:
        summarise(done)
        return 0

    pending = [(q, t, p) for q, t, p in all_cases() if q not in done]
    if not pending:
        summarise(done)
        return 0

    c = Client(args.base_url, "local", timeout=900.0, think=args.think)
    try:
        from app import quick

        snapshot = quick.snapshot()
        ctx = server_ctx(c)
        print(f"n_ctx {ctx}, {len(pending)} case(s) left, running {args.count}")
        for question, expected, param in pending[: args.count]:
            rec = run_one(c, snapshot, question, expected, param)
            append(rec)
            mark = "ok  " if rec["ok"] else "FAIL"
            print(
                f"  {mark} {rec['seconds']:>5.1f}s  {question[:40]:<42} -> {rec['got']}"
            )
            if not rec["ok"]:
                print(f"        {rec['note']}")
    finally:
        c.close()

    summarise(load())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
