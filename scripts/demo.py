#!/usr/bin/env python3
"""Run the synthetic notes-to-MCP demo in an isolated temporary directory."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


def demo_environment(root: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "LOCALRAG_HOME": str(root / "state"),
            "LOCALRAG_NOTES_DIR": str(root / "notes"),
            "LOCALRAG_QUERY_LOG_ROOT": str(root / "query-logs"),
            "LOCALRAG_EMBED_PROVIDER": "fastembed",
            "LOCALRAG_E2E_SPAWN_PYTHON": sys.executable,
        }
    )
    return env


def main() -> int:
    print("research-rag isolated demo (no Zotero or API key required)")
    print("The first run may download the default multilingual embedding model.")
    with tempfile.TemporaryDirectory(prefix="research-rag-demo-") as temp:
        root = Path(temp)
        completed = subprocess.run(
            [
                sys.executable,
                str(REPO_ROOT / "scripts" / "verify_mcp_e2e.py"),
                "--build",
            ],
            cwd=str(REPO_ROOT),
            env=demo_environment(root),
        )
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
