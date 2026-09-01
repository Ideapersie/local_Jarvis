"""Progress checker for the local-LLM migration.

Probes what is actually true on disk, in git, and on the running services, rather
than reading a checklist someone has to remember to tick. A hand-maintained list
drifts from reality exactly when it matters most, part-way through a long job.

Phases match the approved plan at ~/.claude/plans/i-would-like-to-shiny-cake.md.

Usage:
  python scripts/migration_status.py           # fast, no test run
  python scripts/migration_status.py --full    # also runs pytest and ruff
  python scripts/migration_status.py --phase 1 # one phase in detail
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = Path(os.getenv("JARVIS_MODEL_DIR", r"D:\LLM\models"))
LLAMA_DIR = Path(os.getenv("JARVIS_LLAMA_DIR", r"D:\LLM\llama.cpp"))
BENCH_RESULT = ROOT / ".bench_result.json"
LLAMA_URL = os.getenv("JARVIS_LOCAL_BASE_URL", "http://127.0.0.1:8080")

GREEN, YELLOW, RED, DIM, BOLD, OFF = (
    "\033[32m",
    "\033[33m",
    "\033[31m",
    "\033[2m",
    "\033[1m",
    "\033[0m",
)

DONE, TODO, PART = "done", "todo", "part"


@dataclass
class Check:
    label: str
    probe: Callable[[], tuple[str, str]]

    def run(self) -> tuple[str, str]:
        try:
            return self.probe()
        except Exception as exc:  # a broken probe must not hide the rest
            return TODO, f"probe failed: {type(exc).__name__} {exc}"


@dataclass
class Phase:
    number: int
    title: str
    checks: list[Check]


# --- small probe helpers ----------------------------------------------------


def _git(*args: str) -> str:
    out = subprocess.run(
        ["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=30
    )
    return out.stdout.strip()


def _exists(rel: str, note: str = "") -> tuple[str, str]:
    p = ROOT / rel
    if p.exists():
        size = p.stat().st_size
        return DONE, f"{rel} ({size:,} bytes)"
    return TODO, note or f"{rel} not created"


def _grep(rel: str, pattern: str, want: bool = True) -> bool:
    p = ROOT / rel
    if not p.exists():
        return False
    hit = (
        re.search(pattern, p.read_text(encoding="utf-8", errors="replace")) is not None
    )
    return hit is want


def _imports_agent_sdk(rel: str) -> bool:
    return _grep(rel, r"claude_agent_sdk")


# --- phase 0 ----------------------------------------------------------------


def p0_submodule() -> tuple[str, str]:
    gm = ROOT / ".gitmodules"
    if not gm.exists():
        return TODO, "no .gitmodules; brain/ still lives in this repo"
    text = gm.read_text(encoding="utf-8")
    has_brain = "brain" in text
    has_briefs = "briefs" in text
    if has_brain and has_briefs:
        return DONE, "brain/ and briefs/ are submodules"
    if has_brain or has_briefs:
        return PART, f"only {'brain' if has_brain else 'briefs'} is a submodule"
    return TODO, ".gitmodules exists but names neither path"


def p0_history_scrubbed() -> tuple[str, str]:
    commits = _git("log", "--all", "--oneline", "--", "brain", "briefs")
    n = len([ln for ln in commits.splitlines() if ln.strip()])
    if n == 0:
        return DONE, "no commits touch brain/ or briefs/"
    return TODO, f"{n} commits still carry brain/ or briefs/ content"


def p0_repo_private() -> tuple[str, str]:
    # Deliberately offline. A network call here would make the status command
    # slow and flaky; the submodule check above is the load-bearing one.
    remote = _git("remote", "get-url", "origin")
    return PART, f"verify manually: {remote or 'no origin'}"


def p0_no_secrets() -> tuple[str, str]:
    leaked = _git(
        "log",
        "--all",
        "--full-history",
        "--oneline",
        "--",
        ".env",
        "credentials.json",
        "token.json",
    )
    if leaked.strip():
        return TODO, "SECRETS IN HISTORY, rotate now"
    return DONE, ".env / credentials.json / token.json never committed"


def p0_imported_dir() -> tuple[str, str]:
    d = ROOT / "brain" / "imported"
    if not d.exists():
        return TODO, "brain/imported/ not created"
    n = len(list(d.rglob("*.md")))
    return (DONE, f"{n} imported memory files") if n else (PART, "exists but empty")


# --- phase 1 ----------------------------------------------------------------


def p1_llama_installed() -> tuple[str, str]:
    exe = (
        next(LLAMA_DIR.rglob("llama-server.exe"), None) if LLAMA_DIR.exists() else None
    )
    if exe:
        return DONE, str(exe)
    return TODO, f"llama-server.exe not under {LLAMA_DIR}"


def p1_model_present() -> tuple[str, str]:
    if not MODEL_DIR.exists():
        return TODO, f"{MODEL_DIR} does not exist"
    ggufs = sorted(MODEL_DIR.rglob("*.gguf"), key=lambda p: -p.stat().st_size)
    if not ggufs:
        return TODO, f"no .gguf under {MODEL_DIR}"
    lines = [f"{p.name} ({p.stat().st_size / 1e9:.1f} GB)" for p in ggufs[:3]]
    return DONE, "; ".join(lines)


def p1_server_up() -> tuple[str, str]:
    try:
        import httpx

        r = httpx.get(f"{LLAMA_URL}/health", timeout=2.0)
        return (
            (DONE, f"llama-server responding at {LLAMA_URL}")
            if r.status_code < 500
            else (PART, f"{LLAMA_URL} returned {r.status_code}")
        )
    except Exception:
        return TODO, f"nothing listening on {LLAMA_URL}"


def p1_bench_written() -> tuple[str, str]:
    return _exists("scripts/bench_local_model.py")


def p1_bench_passed() -> tuple[str, str]:
    if not BENCH_RESULT.exists():
        return TODO, "bench never run (no .bench_result.json)"
    data = json.loads(BENCH_RESULT.read_text(encoding="utf-8"))
    when = data.get("when", "?")
    if data.get("gate_pass"):
        return DONE, f"gate passed {when}"
    fails = ", ".join(data.get("failures", [])) or "see output"
    return TODO, f"gate FAILED {when}: {fails}"


# --- phases 2 to 7 ----------------------------------------------------------


def p2_config() -> tuple[str, str]:
    bits = {
        "LOCAL_BASE_URL": _grep("app/config.py", r"LOCAL_BASE_URL"),
        "opus portfolio": _grep(
            "app/config.py", r'MODEL_PORTFOLIO\s*=\s*"claude-opus-5"'
        ),
        "auth cruft gone": _grep("app/config.py", r"AGENT_AUTH", want=False),
    }
    done = [k for k, v in bits.items() if v]
    if len(done) == len(bits):
        return DONE, "config migrated"
    if not done:
        return TODO, "config untouched"
    return PART, f"done: {', '.join(done)}"


def p4_sdk_gone() -> tuple[str, str]:
    users = [
        str(p.relative_to(ROOT)).replace("\\", "/")
        for p in (ROOT / "app").rglob("*.py")
        if _imports_agent_sdk(str(p.relative_to(ROOT)).replace("\\", "/"))
    ]
    if not users:
        return DONE, "no app/ module imports claude_agent_sdk"
    return TODO, f"still importing: {', '.join(sorted(users))}"


def p5_callers() -> tuple[str, str]:
    callers = {
        "chat": "app/routers/chat.py",
        "brief": "app/services/brief_builder.py",
        "career": "app/routers/career.py",
        "jobs": "app/jobs.py",
        "triage": "app/services/triage.py",
        "quick": "app/quick.py",
    }
    moved = [
        name
        for name, rel in callers.items()
        if _grep(rel, r"app\.llm|app\.loop|from app import loop")
    ]
    if len(moved) == len(callers):
        return DONE, "all callers repointed"
    if not moved:
        return TODO, "none repointed"
    return PART, f"{len(moved)}/{len(callers)}: {', '.join(moved)}"


def p6_panel_wired() -> tuple[str, str]:
    if _grep("app/main.py", r'"portfolio":\s*None'):
        return TODO, "app/main.py still passes portfolio=None"
    return DONE, "portfolio placeholder replaced"


def p7_sync_run() -> tuple[str, str]:
    d = ROOT / "brain" / "imported"
    if not (ROOT / "scripts/sync_claude_memory.py").exists():
        return TODO, "sync script not written"
    if not d.exists() or not any(d.rglob("*.md")):
        return PART, "script exists but has never populated brain/imported/"
    return DONE, f"{len(list(d.rglob('*.md')))} files synced"


PHASES = [
    Phase(
        0,
        "Split the brain out of the public repo",
        [
            Check("no secrets in git history", p0_no_secrets),
            Check("brain/ + briefs/ are submodules", p0_submodule),
            Check("history scrubbed of brain/briefs", p0_history_scrubbed),
            Check("brain/imported/ exists", p0_imported_dir),
            Check("public repo reviewed", p0_repo_private),
        ],
    ),
    Phase(
        1,
        "Runtime, model, and the bench gate",
        [
            Check("llama.cpp installed", p1_llama_installed),
            Check("GGUF downloaded", p1_model_present),
            Check("bench script written", p1_bench_written),
            Check("llama-server running", p1_server_up),
            Check("BENCH GATE passed", p1_bench_passed),
        ],
    ),
    Phase(
        2,
        "Provider abstraction",
        [
            Check("app/llm/base.py", lambda: _exists("app/llm/base.py")),
            Check("app/llm/local.py", lambda: _exists("app/llm/local.py")),
            Check("app/llm/remote.py", lambda: _exists("app/llm/remote.py")),
            Check("config migrated", p2_config),
        ],
    ),
    Phase(
        3,
        "Tools the loop can call",
        [
            Check("app/llm/tools.py", lambda: _exists("app/llm/tools.py")),
            Check("app/llm/fs_tools.py", lambda: _exists("app/llm/fs_tools.py")),
            Check(
                "app/integrations/search.py",
                lambda: _exists("app/integrations/search.py"),
            ),
        ],
    ),
    Phase(
        4,
        "The agent loop",
        [
            Check("app/loop.py", lambda: _exists("app/loop.py")),
            Check("claude_agent_sdk removed from app/", p4_sdk_gone),
        ],
    ),
    Phase(
        5,
        "Repoint the callers",
        [
            Check("callers use the new loop", p5_callers),
        ],
    ),
    Phase(
        6,
        "Portfolio analysis on Opus 5",
        [
            Check(
                "app/integrations/trading212.py",
                lambda: _exists("app/integrations/trading212.py"),
            ),
            Check(
                "app/services/portfolio.py",
                lambda: _exists("app/services/portfolio.py"),
            ),
            Check(
                "app/routers/portfolio.py", lambda: _exists("app/routers/portfolio.py")
            ),
            Check("dashboard panel wired", p6_panel_wired),
        ],
    ),
    Phase(
        7,
        "Claude memory sync",
        [
            Check(
                "scripts/sync_claude_memory.py",
                lambda: _exists("scripts/sync_claude_memory.py"),
            ),
            Check("sync has run", p7_sync_run),
        ],
    ),
]


# --- health ------------------------------------------------------------------


def run_health() -> list[tuple[str, str, str]]:
    out = []
    py = ROOT / ".venv" / "Scripts" / "python.exe"
    py = str(py) if py.exists() else sys.executable

    r = subprocess.run(
        [py, "-m", "pytest", "--tb=no", "-q", "-p", "no:cacheprovider"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=600,
    )
    tail = [ln for ln in r.stdout.splitlines() if "passed" in ln or "failed" in ln]
    out.append(
        (
            "pytest",
            DONE if r.returncode == 0 else TODO,
            tail[-1] if tail else "see output",
        )
    )

    r = subprocess.run(
        [py, "-m", "ruff", "check", "--output-format=concise", "."],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    n = len([ln for ln in r.stdout.splitlines() if ": " in ln and ln[0].isalpha()])
    out.append(
        ("ruff", DONE if r.returncode == 0 else PART, f"{n} findings" if n else "clean")
    )
    return out


# --- rendering ---------------------------------------------------------------

MARK = {DONE: (GREEN, "[x]"), PART: (YELLOW, "[~]"), TODO: (RED, "[ ]")}


def bar(done: float, width: int = 28) -> str:
    filled = round(done * width)
    return "#" * filled + "." * (width - filled)


def main() -> int:
    ap = argparse.ArgumentParser(description="Local-LLM migration progress.")
    ap.add_argument("--full", action="store_true", help="also run pytest and ruff")
    ap.add_argument("--phase", type=int, help="show only this phase")
    ap.add_argument("--no-color", action="store_true")
    args = ap.parse_args()

    if args.no_color or not sys.stdout.isatty():
        globals().update(
            dict.fromkeys(["GREEN", "YELLOW", "RED", "DIM", "BOLD", "OFF"], "")
        )
        for k in MARK:
            MARK[k] = ("", MARK[k][1])

    phases = [p for p in PHASES if args.phase is None or p.number == args.phase]

    print()
    print(f"{BOLD}Jarvis local-LLM migration{OFF}")
    print(f"{DIM}plan: ~/.claude/plans/i-would-like-to-shiny-cake.md{OFF}")
    print()

    weight_done = weight_total = 0.0
    for ph in phases:
        results = [(c.label, *c.run()) for c in ph.checks]
        score = sum({DONE: 1.0, PART: 0.5, TODO: 0.0}[s] for _, s, _ in results)
        weight_done += score
        weight_total += len(results)
        pct = score / len(results) if results else 0.0

        head = GREEN if pct == 1 else (YELLOW if pct > 0 else DIM)
        print(f"{head}{BOLD}Phase {ph.number}{OFF} {head}{ph.title}{OFF}")
        print(f"  {DIM}{bar(pct)}{OFF} {pct:>4.0%}")
        for label, state, detail in results:
            colour, mark = MARK[state]
            print(f"  {colour}{mark}{OFF} {label:<34} {DIM}{detail}{OFF}")
        print()

    if args.full:
        print(f"{BOLD}Health{OFF}")
        for name, state, detail in run_health():
            colour, mark = MARK[state]
            print(f"  {colour}{mark}{OFF} {name:<34} {DIM}{detail}{OFF}")
        print()

    if args.phase is None:
        overall = weight_done / weight_total if weight_total else 0.0
        print(
            f"{BOLD}Overall{OFF}  {bar(overall, 40)} {overall:.0%}"
            f"  {DIM}({weight_done:g}/{weight_total:g} checks){OFF}"
        )
        nxt = next(
            (p for p in PHASES if any(c.run()[0] != DONE for c in p.checks)), None
        )
        if nxt:
            pending = [c.label for c in nxt.checks if c.run()[0] != DONE]
            print(f"{BOLD}Next{OFF}     Phase {nxt.number}: {pending[0]}")
        else:
            print(f"{GREEN}{BOLD}Migration complete.{OFF}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
