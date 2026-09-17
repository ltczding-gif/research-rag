#!/usr/bin/env python3
"""Build both research-rag Chroma collections with one command."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent


def build_commands(
    python_executable: str,
    rebuild_papers: bool = False,
    allow_removals: bool = False,
    notes_only: bool = False,
    rebuild_notes: bool = False,
) -> list[list[str]]:
    notes = [python_executable, str(REPO_ROOT / "service" / "build_notes_db.py")]
    if rebuild_notes:
        notes.append("--rebuild")
    if allow_removals:
        notes.append("--allow-removals")
    commands = [notes]
    if not notes_only:
        papers = [python_executable, str(REPO_ROOT / "service" / "build_pdf_db.py")]
        if rebuild_papers:
            papers.append("--rebuild")
        if allow_removals:
            papers.append("--allow-removals")
        commands.append(papers)
    return commands


def run_builds(
    commands: Sequence[Sequence[str]],
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    total = len(commands)
    for index, command in enumerate(commands, 1):
        label = "notes" if Path(command[1]).name == "build_notes_db.py" else "paper chunks"
        print(f"[{index}/{total}] Building {label} index...", flush=True)
        completed = runner(list(command), cwd=str(REPO_ROOT))
        if completed.returncode == 3:
            print(f"[COMMITTED WITH WARNING] {label} committed. Inspect status; remaining builds not started.", file=sys.stderr)
            return 3
        if completed.returncode != 0:
            print(f"[ERROR] {label} index failed (exit {completed.returncode})", file=sys.stderr)
            return completed.returncode or 1
    print("[OK] Requested indexes are ready.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebuild-papers",
        action="store_true",
        help="Build a fresh papers candidate even when the current inputs are unchanged.",
    )
    parser.add_argument(
        "--allow-removals",
        action="store_true",
        help="Allow sources absent from the current full snapshots to be withdrawn.",
    )
    parser.add_argument(
        "--notes-only",
        action="store_true",
        help="Build only the notes index.",
    )
    parser.add_argument("--rebuild-notes", action="store_true",
                        help="Force a fresh notes candidate without deleting active data.")
    args = parser.parse_args(argv)
    if args.notes_only and args.rebuild_papers:
        parser.error("--rebuild-papers cannot be used with --notes-only")
    return run_builds(
        build_commands(
            sys.executable,
            args.rebuild_papers,
            args.allow_removals,
            args.notes_only,
            args.rebuild_notes,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
