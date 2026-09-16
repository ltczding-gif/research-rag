#!/usr/bin/env python3
"""Run the synthetic notes-to-MCP demo in an isolated temporary directory."""

from __future__ import annotations

import argparse
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
            "LOCALRAG_CHROMA_PATH": str(root / "state" / "chroma"),
            "LOCALRAG_NOTES_DIR": str(root / "notes"),
            "LOCALRAG_PDF_LEDGER": str(root / "state" / "processed_groups.txt"),
            "LOCALRAG_NOTES_LEDGER": str(root / "state" / "processed_notes.txt"),
            "LOCALRAG_TEXTBOOK_LEDGER": str(root / "state" / "textbook_ledger.txt"),
            "LOCALRAG_NOTES_COLLECTION": "notes",
            "LOCALRAG_PAPERS_COLLECTION": "papers",
            "LOCALRAG_QUERY_LOG_ROOT": str(root / "query-logs"),
            "LOCALRAG_EMBED_PROVIDER": "fastembed",
            "LOCALRAG_FASTEMBED_MODEL": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            "LOCALRAG_E2E_SPAWN_PYTHON": sys.executable,
        }
    )
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--direct-server",
        action="store_true",
        help="Run the MCP server directly under this Python instead of through the launcher.",
    )
    args = parser.parse_args()
    print("research-rag isolated demo (no Zotero or API key required)")
    print("The first run may download the default multilingual embedding model.")
    with tempfile.TemporaryDirectory(prefix="research-rag-demo-") as temp:
        root = Path(temp)
        command = [
            sys.executable,
            str(REPO_ROOT / "scripts" / "verify_mcp_e2e.py"),
            "--build",
        ]
        if args.direct_server:
            command.append("--direct-server")
        completed = subprocess.run(
            command,
            cwd=str(REPO_ROOT),
            env=demo_environment(root),
        )
        return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
