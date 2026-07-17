#!/usr/bin/env python3
"""
Cross-platform launcher for the research-rag MCP server.

`.mcp.json` can't portably reference `.venv/bin/python` (POSIX) vs
`.venv\\Scripts\\python.exe` (Windows), so it invokes this launcher with
whatever `python` the host resolves. The launcher then re-executes the
actual server under the repo venv when one exists; otherwise it runs the
server in-process with the current interpreter.

stdio passes through untouched, which is all the MCP transport needs.
"""

from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVER = REPO_ROOT / "service" / "mcp_server.py"

_VENV_PYTHONS = [
    REPO_ROOT / ".venv" / "Scripts" / "python.exe",  # Windows, default layout
    REPO_ROOT / ".venv" / "bin" / "python",          # POSIX, default layout
    REPO_ROOT / "service" / ".venv" / "Scripts" / "python.exe",  # --isolated
    REPO_ROOT / "service" / ".venv" / "bin" / "python",
]


def main() -> int:
    current = Path(sys.executable).resolve()
    for candidate in _VENV_PYTHONS:
        if candidate.exists():
            if candidate.resolve() == current:
                break  # already running under the venv
            completed = subprocess.run(
                [str(candidate), str(SERVER)],
                cwd=str(REPO_ROOT),
            )
            return completed.returncode
    # No venv found (or we are already inside it): run in-process.
    sys.argv = [str(SERVER)]
    runpy.run_path(str(SERVER), run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
