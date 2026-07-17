#!/usr/bin/env python3
"""Build both research-rag Chroma collections with one command."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence


REPO_ROOT = Path(__file__).resolve().parent.parent


def build_commands(python_executable: str, rebuild_papers: bool = False) -> list[list[str]]:
    commands = [
        [python_executable, str(REPO_ROOT / "service" / "build_notes_db.py")],
        [python_executable, str(REPO_ROOT / "service" / "build_pdf_db.py")],
    ]
    if rebuild_papers:
        commands[1].append("--rebuild")
    return commands


def run_builds(
    commands: Sequence[Sequence[str]],
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> int:
    for index, command in enumerate(commands, 1):
        label = "notes" if index == 1 else "paper chunks"
        print(f"[{index}/2] Building {label} index...", flush=True)
        completed = runner(list(command), cwd=str(REPO_ROOT))
        if completed.returncode != 0:
            print(f"[ERROR] {label} index failed (exit {completed.returncode})", file=sys.stderr)
            return completed.returncode or 1
    print("[OK] Both indexes are ready.")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--rebuild-papers",
        action="store_true",
        help="Explicitly reset and rebuild only the papers collection before ingestion.",
    )
    args = parser.parse_args(argv)
    return run_builds(build_commands(sys.executable, args.rebuild_papers))


if __name__ == "__main__":
    raise SystemExit(main())
