"""
Smoke test for the stdio MCP server (service/mcp_server.py).

Runs the import + tool registration in a SUBPROCESS because scanner/ and
service/ both ship a top-level `config` module — inside one pytest process
whichever got imported first would win. The subprocess also mirrors how a
real MCP host spawns the server.

Skips only in deliberately minimal local environments where the service deps
(mcp / chromadb / flask) are absent. The CI matrix installs the full default
requirements set, so this contract is mandatory there.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")
pytest.importorskip("chromadb")
pytest.importorskip("flask")

REPO_ROOT = Path(__file__).resolve().parent.parent

_PROBE = r"""
import asyncio
import json
import sys
sys.path.insert(0, r"{service_dir}")
import mcp_server

tools = asyncio.run(mcp_server.mcp.list_tools())
print(json.dumps(sorted(t.name for t in tools)))
"""


def test_mcp_server_registers_expected_tools():
    env = os.environ.copy()
    env["LOCALRAG_SKIP_CHROMA_INIT"] = "1"
    completed = subprocess.run(
        [sys.executable, "-c", _PROBE.format(service_dir=str(REPO_ROOT / "service"))],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, (
        f"mcp_server import failed.\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    import json

    names = set(json.loads(completed.stdout.strip().splitlines()[-1]))
    assert {"search_notes", "search_papers", "get_note", "index_status"} <= names
