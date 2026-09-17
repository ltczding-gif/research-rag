#!/usr/bin/env python3
"""Smoke-test the README default install and real stdio MCP launcher.

The check stops at MCP tool discovery. It does not download a model, build an
index, call an API, or make a retrieval-quality claim.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import version as package_version
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = ROOT / "scripts" / "run_mcp_server.py"
SERVER = ROOT / "service" / "mcp_server.py"
TOOLS = {"search_notes", "search_papers", "get_note", "index_status", "prepare_answer", "check_answer"}
BASE_ENV = "LOCALRAG_INSTALL_SMOKE_BASE_PYTHON"
TRACE_ENV = "LOCALRAG_INSTALL_SMOKE_TRACE"

TRACE_HOOK = '''import json
import os
import sys

trace = os.environ.get("LOCALRAG_INSTALL_SMOKE_TRACE")
if trace:
    with open(trace, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({
            "executable": sys.executable,
            "prefix": sys.prefix,
            "base_prefix": sys.base_prefix,
            "argv": sys.argv,
        }) + "\\n")
'''


def _path(value: str | Path) -> str:
    """Normalize without dereferencing a venv Python symlink."""
    return os.path.normcase(os.path.abspath(os.fspath(value)))


def _venv_python() -> Path:
    for candidate in (
        ROOT / ".venv" / "Scripts" / "python.exe",
        ROOT / ".venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return candidate
    raise RuntimeError(
        "default .venv is missing; run setup.sh --no-init or "
        "setup.ps1 -SkipInit first"
    )


async def _discover_tools(base_python: Path, env: dict[str, str]) -> set[str]:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    params = StdioServerParameters(
        command=str(base_python),
        args=[str(LAUNCHER)],
        cwd=str(ROOT),
        env=env,
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            response = await session.list_tools()
            return {tool.name for tool in response.tools}


def _verify(venv_python: Path) -> int:
    base_python = Path(os.environ[BASE_ENV])
    if not base_python.is_file() or _path(base_python) == _path(venv_python):
        raise RuntimeError("base and repository-venv interpreters must both exist")
    if _path(sys.executable) != _path(venv_python):
        raise RuntimeError(f"client is outside repository .venv: {sys.executable}")

    with tempfile.TemporaryDirectory(prefix="research-rag-install-smoke-") as raw:
        temp = Path(raw)
        hook_dir = temp / "trace-hook"
        hook_dir.mkdir()
        (hook_dir / "sitecustomize.py").write_text(TRACE_HOOK, encoding="utf-8")
        trace_path = temp / "python-processes.jsonl"
        env = os.environ.copy()
        env.update(
            {
                "LOCALRAG_HOME": str(temp / "localrag"),
                "LOCALRAG_CHROMA_PATH": str(temp / "localrag" / "chroma"),
                "LOCALRAG_NOTES_DIR": str(temp / "notes"),
                "LOCALRAG_SKIP_CHROMA_INIT": "1",
                "PYTHONIOENCODING": "utf-8",
                "PYTHONUTF8": "1",
                "PYTHONPATH": str(hook_dir),
                TRACE_ENV: str(trace_path),
            }
        )

        tools = asyncio.run(
            asyncio.wait_for(_discover_tools(base_python, env), timeout=90)
        )
        missing = TOOLS - tools
        if missing:
            raise RuntimeError(
                f"missing MCP tools {sorted(missing)}; got {sorted(tools)}"
            )

        rows = [
            json.loads(line)
            for line in trace_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

        def find(executable: Path, entrypoint: Path) -> dict[str, object] | None:
            for row in rows:
                argv = row.get("argv")
                if (
                    isinstance(argv, list)
                    and argv
                    and _path(str(row.get("executable", ""))) == _path(executable)
                    and _path(str(argv[0])) == _path(entrypoint)
                ):
                    return row
            return None

        base_row = find(base_python, LAUNCHER)
        server_row = find(venv_python, SERVER)
        if base_row is None or server_row is None:
            raise RuntimeError(f"base-to-venv process trace is incomplete: {rows}")
        if _path(str(base_row["prefix"])) != _path(str(base_row["base_prefix"])):
            raise RuntimeError(f"launcher did not start in the base environment: {rows}")
        if (
            _path(str(server_row["prefix"])) != _path(venv_python.parent.parent)
            or _path(str(server_row["prefix"]))
            == _path(str(server_row["base_prefix"]))
        ):
            raise RuntimeError(f"server did not enter the repository venv: {rows}")

    print(
        "PASS: base launcher "
        f"executable={base_row['executable']} prefix={base_row['prefix']}"
    )
    print(
        "PASS: venv server "
        f"executable={server_row['executable']} prefix={server_row['prefix']} "
        f"base_prefix={server_row['base_prefix']}"
    )
    print(
        "PASS: installed versions "
        f"python={sys.version.split()[0]} mcp={package_version('mcp')} "
        f"chromadb={package_version('chromadb')} "
        f"fastembed={package_version('fastembed')}"
    )
    print(f"PASS: discovered MCP tools: {sorted(tools)}")
    print("Scope: installation, launcher re-exec, and MCP tool discovery only")
    return 0


def main() -> int:
    venv_python = _venv_python()
    if BASE_ENV in os.environ:
        return _verify(venv_python)
    env = os.environ.copy()
    env[BASE_ENV] = sys.executable
    return subprocess.run(
        [str(venv_python), str(Path(__file__).resolve())],
        cwd=str(ROOT),
        env=env,
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
