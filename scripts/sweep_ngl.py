r"""Find the -ngl that actually generates fastest on this GPU.

More layers on the GPU is not monotonically better. Past roughly 95% VRAM the
NVIDIA driver starts backing allocations with shared system memory, and that
path is slower than llama.cpp's own CPU offload - so the curve turns over and
pushing -ngl higher makes generation worse while prefill still looks better.

Generation tok/s is the number that matters here: the brief and every chat turn
are output-bound, and prefill is amortised by the prompt cache.

Usage:
  python scripts/sweep_ngl.py --ngl 32 36 40 44 --ctx 8192
"""

from __future__ import annotations

import argparse
import os
import subprocess
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = Path(os.getenv("JARVIS_MODEL_DIR", r"D:\LLM\models"))
LLAMA_DIR = Path(os.getenv("JARVIS_LLAMA_DIR", r"D:\LLM\llama.cpp"))
URL = "http://127.0.0.1:8080"

PROMPT = "Count from 1 to 40, one number per line, nothing else."


def find(pattern: str, root: Path) -> Path | None:
    return next(root.rglob(pattern), None) if root.exists() else None


def vram_used_mib() -> int:
    out = subprocess.run(
        ["nvidia-smi", "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    try:
        return int(out.stdout.strip().splitlines()[0])
    except (ValueError, IndexError):
        return -1


def kill_server() -> None:
    subprocess.run(
        ["taskkill", "/F", "/IM", "llama-server.exe"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    time.sleep(3)


def wait_ready(deadline_s: float = 300.0) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            if httpx.get(f"{URL}/health", timeout=2.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(4)
    return False


def measure(max_tokens: int) -> tuple[float, float] | None:
    try:
        r = httpx.post(
            f"{URL}/v1/chat/completions",
            json={
                "messages": [{"role": "user", "content": PROMPT}],
                "max_tokens": max_tokens,
                "temperature": 0,
            },
            timeout=1800,
        )
        t = r.json().get("timings") or {}
        gen = t["predicted_n"] / (t["predicted_ms"] / 1000.0)
        pre = t["prompt_n"] / (t["prompt_ms"] / 1000.0)
        return pre, gen
    except (httpx.HTTPError, KeyError, ZeroDivisionError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="Sweep -ngl for best generation speed.")
    ap.add_argument("--ngl", type=int, nargs="+", default=[28, 32, 36, 40, 44])
    ap.add_argument("--ctx", type=int, default=8192)
    ap.add_argument("--max-tokens", type=int, default=120)
    ap.add_argument("--model", default="")
    args = ap.parse_args()

    server = find("llama-server.exe", LLAMA_DIR)
    if not server:
        print(f"llama-server.exe not found under {LLAMA_DIR}")
        return 2
    gguf = (
        MODEL_DIR / args.model
        if args.model
        else max(
            MODEL_DIR.rglob("*.gguf"), key=lambda p: p.stat().st_size, default=None
        )
    )
    if not gguf or not gguf.exists():
        print(f"no model found under {MODEL_DIR}")
        return 2

    print(f"model {gguf.name}, ctx {args.ctx}\n")
    print(f"{'ngl':>5} {'VRAM MiB':>9} {'prefill':>9} {'generate':>9}")
    print("-" * 36)

    results: list[tuple[int, float]] = []
    for ngl in args.ngl:
        kill_server()
        proc = subprocess.Popen(
            [
                str(server),
                "-m",
                str(gguf),
                "-ngl",
                str(ngl),
                "-c",
                str(args.ctx),
                "--host",
                "127.0.0.1",
                "--port",
                "8080",
                "--jinja",
                "--cache-type-k",
                "q8_0",
                "--cache-type-v",
                "q8_0",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            if not wait_ready():
                print(f"{ngl:>5} {'':>9} {'did not start (likely OOM)':>9}")
                continue
            vram = vram_used_mib()
            got = measure(args.max_tokens)
            if got is None:
                print(f"{ngl:>5} {vram:>9} {'request failed':>19}")
                continue
            pre, gen = got
            results.append((ngl, gen))
            print(f"{ngl:>5} {vram:>9} {pre:>8.2f}/s {gen:>8.2f}/s")
        finally:
            proc.terminate()

    kill_server()
    if results:
        best_ngl, best_gen = max(results, key=lambda r: r[1])
        print()
        print(f"Best generation: -ngl {best_ngl} at {best_gen:.2f} tok/s")
        print(
            f"Relaunch with:  scripts\\start-llama.ps1 -Ngl {best_ngl} -Ctx {args.ctx}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
