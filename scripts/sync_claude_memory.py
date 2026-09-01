r"""Copy Claude Code's curated memories into brain/imported/.

One way, always. Claude Code owns ~/.claude/projects/*/memory/, and writing back
into a store another process is actively using would race.

What this syncs and what it deliberately does not. Each project folder holds a
memory/ directory of curated .md facts alongside raw .jsonl session transcripts.
Only the .md files come across. The transcripts are enormous - this project
alone has dozens - and dropping them into brain/ would bury the six files the
brief writer actually reads. The memory files are already distilled to one fact
each, which is the right unit.

Conversations in the claude.ai web or desktop app are not on disk and have no
export API, so they cannot be synced here at all. This covers Claude Code
sessions on this machine, which is where the Jarvis work happens.

Idempotent: a second run with nothing changed writes nothing and commits
nothing.

Usage:
  python scripts/sync_claude_memory.py            # copy, commit, push
  python scripts/sync_claude_memory.py --dry-run  # show what would change
  python scripts/sync_claude_memory.py --no-push  # commit locally only
"""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config

log = logging.getLogger("jarvis.sync_memory")

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
IMPORTED = config.BRAIN_DIR / "imported"

HEADER = """# Imported memories

Synced from Claude Code by scripts/sync_claude_memory.py. One folder per project
slug. Do not hand-edit: anything here is overwritten on the next sync. The
hand-maintained files live one level up, and keeping them separate is what makes
a git diff show plainly which is which.
"""


def _slug_dirs() -> list[Path]:
    if not CLAUDE_PROJECTS.is_dir():
        return []
    return sorted(p for p in CLAUDE_PROJECTS.iterdir() if (p / "memory").is_dir())


def plan() -> list[tuple[Path, Path]]:
    """(source, destination) for every memory file whose content differs."""
    jobs: list[tuple[Path, Path]] = []
    for project in _slug_dirs():
        for src in sorted((project / "memory").glob("*.md")):
            dst = IMPORTED / project.name / src.name
            try:
                same = dst.exists() and dst.read_bytes() == src.read_bytes()
            except OSError:
                same = False
            if not same:
                jobs.append((src, dst))
    return jobs


def stale(kept: set[Path]) -> list[Path]:
    """Files under imported/ whose source memory no longer exists."""
    if not IMPORTED.is_dir():
        return []
    return [
        p
        for p in IMPORTED.rglob("*.md")
        if p not in kept and p.name != "README.md"
    ]


def _git(*args: str, cwd: Path) -> tuple[int, str]:
    out = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=120
    )
    return out.returncode, (out.stdout + out.stderr).strip()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Sync Claude Code memories into brain/.")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-push", action="store_true")
    args = ap.parse_args()

    projects = _slug_dirs()
    if not projects:
        log.info("no Claude Code project memories found under %s", CLAUDE_PROJECTS)
        return 0

    jobs = plan()
    expected = {
        IMPORTED / p.name / f.name
        for p in projects
        for f in (p / "memory").glob("*.md")
    }
    orphans = stale(expected)

    log.info(
        "%d project(s), %d file(s) to copy, %d stale to remove",
        len(projects),
        len(jobs),
        len(orphans),
    )
    for _, dst in jobs:
        log.info("  + %s", dst.relative_to(config.BRAIN_DIR))
    for path in orphans:
        log.info("  - %s", path.relative_to(config.BRAIN_DIR))

    if args.dry_run:
        return 0
    if not jobs and not orphans:
        log.info("nothing changed")
        return 0

    IMPORTED.mkdir(parents=True, exist_ok=True)
    readme = IMPORTED / "README.md"
    if not readme.exists():
        readme.write_text(HEADER, encoding="utf-8")

    for src, dst in jobs:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    for path in orphans:
        path.unlink()

    # brain/ is a junction into the private jarvis-brain repo, so the commit
    # belongs there, not in local_Jarvis. Resolving the path is what finds it.
    repo = config.BRAIN_DIR.resolve().parent
    code, out = _git("add", "-A", cwd=repo)
    if code != 0:
        log.error("git add failed: %s", out)
        return 1

    code, out = _git("status", "--porcelain", cwd=repo)
    if not out.strip():
        log.info("files copied but git sees no change")
        return 0

    code, out = _git(
        "commit",
        "-m",
        f"Sync {len(jobs)} Claude Code memory file(s)",
        cwd=repo,
    )
    if code != 0:
        log.error("git commit failed: %s", out)
        return 1
    log.info("committed to %s", repo.name)

    if args.no_push:
        return 0
    code, out = _git("push", cwd=repo)
    if code != 0:
        # Not fatal: the memories are committed locally and the next run pushes.
        log.warning("push failed, will retry next run: %s", out)
        return 0
    log.info("pushed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
