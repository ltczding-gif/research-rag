#!/usr/bin/env python3
"""
End-to-end verification of the research-rag stdio MCP retrieval path.

This is a *reproducible acceptance tool*, not a unit test. It spawns the
real MCP server as a subprocess (through scripts/run_mcp_server.py, using a
NON-venv Python so the launcher's re-exec-into-.venv logic is exercised for
real), talks JSON-RPC over stdio via the `mcp` client SDK, and asserts the
full tool surface behaves against an isolated synthetic corpus.

WHY a separate script (vs. a pytest case):
  * It needs a *real* ChromaDB built by build_notes_db.py, a real fastembed
    model, and a real subprocess re-exec — none of which belong in the
    fast, dependency-optional unit suite.
  * Spawning a *different* interpreter than the one running the client is
    the whole point (proves .mcp.json's `command: "python"` works even when
    PATH only has a non-venv Python).

SAFETY: refuses to run unless LOCALRAG_HOME / LOCALRAG_NOTES_DIR are set to
a temp/throwaway location (never the user's real ~/.localrag). Every process
this script starts inherits those isolated paths.

Usage:
    # One-shot: build the synthetic corpus + DB, then verify.
    LOCALRAG_HOME=/tmp/e2e_home \
    LOCALRAG_NOTES_DIR=/tmp/e2e_notes \
    LOCALRAG_QUERY_LOG_ROOT=/tmp/e2e_notes/_query_logs \
    LOCALRAG_EMBED_PROVIDER=fastembed \
    python scripts/verify_mcp_e2e.py --build

    # Verify only (corpus/DB already built by a prior --build):
    python scripts/verify_mcp_e2e.py

Exit code: 0 = all steps PASS, 1 = any step FAILED (or setup error).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LAUNCHER = REPO_ROOT / "scripts" / "run_mcp_server.py"
BUILD_NOTES_DB = REPO_ROOT / "service" / "build_notes_db.py"

# The Chinese query that must rank the OER note first. "氧析出反应" (oxygen
# evolution) is deliberately close to the fuel-cell note's "氧还原反应"
# (oxygen reduction) so a hit on OER-first proves real semantic
# discrimination, not keyword overlap.
E2E_QUERY = "氧析出反应催化剂"
E2E_EXPECT_TOP_SOURCE = "oer_iridium_review_note.md"

# --- Synthetic corpus: 3 distinct electrochemistry topics -------------------
# Frontmatter mirrors the real schema read by build_notes_db.py /
# query_server.py (zotero_parent_key required; title_en/title_zh/year/
# journal/doi/authors optional).

_CORPUS: dict[str, str] = {
    "oer_iridium_review_note.md": """---
zotero_parent_key: OER00001
title_en: "Iridium Oxide Catalysts for the Oxygen Evolution Reaction in Acidic Water Electrolysis"
title_zh: "酸性水电解中用于氧析出反应的氧化铱催化剂"
year: 2023
journal: "Nature Catalysis"
doi: 10.1000/oer.2023.001
authors:
  - Zhang Wei
  - Li Ming
---

# 氧析出反应 (OER) 催化剂研究笔记

本文研究酸性介质中析氧反应 (oxygen evolution reaction, OER) 的电催化剂。
重点考察 IrO2 基催化剂在质子交换膜水电解 (PEM water electrolysis) 阳极的
过电位与稳定性。作者通过调控晶格氧机制 (lattice oxygen mechanism) 降低
Tafel 斜率，在 10 mA/cm² 电流密度下析氧过电位仅 260 mV，并保持 1000 小时稳定。
关键词：析氧反应、氧化铱、酸性水电解、阳极过电位、催化剂稳定性。
""",
    "co2rr_copper_review_note.md": """---
zotero_parent_key: CO2R0002
title_en: "Copper Catalysts for Electrochemical CO2 Reduction to Multi-carbon Products"
title_zh: "用于电化学二氧化碳还原制多碳产物的铜催化剂"
year: 2022
journal: "Journal of the American Chemical Society"
doi: 10.1000/co2rr.2022.002
authors:
  - Wang Fang
---

# 二氧化碳电还原 (CO2RR) 催化剂研究笔记

本文聚焦电化学二氧化碳还原反应 (CO2 reduction reaction, CO2RR)。
铜基催化剂通过 C-C 偶联在 CO2RR 中生成乙烯和乙醇等多碳 (C2+) 产物。
作者优化了 Cu 晶面取向与局部 pH，使乙烯法拉第效率达到 65%。
关键词：二氧化碳还原、铜催化剂、多碳产物、乙烯、法拉第效率。
""",
    "pemfc_ptco_review_note.md": """---
zotero_parent_key: FCELL003
title_en: "Platinum-Cobalt Alloy Cathode Catalysts for Proton-Exchange-Membrane Fuel Cells"
title_zh: "质子交换膜燃料电池的铂钴合金阴极催化剂"
year: 2021
journal: "Science"
doi: 10.1000/fc.2021.003
authors:
  - Chen Hao
---

# 质子交换膜燃料电池 (PEMFC) 研究笔记

本文研究质子交换膜燃料电池 (proton exchange membrane fuel cell, PEMFC)
阴极的氧还原反应 (oxygen reduction reaction, ORR) 铂合金催化剂。
通过 Pt-Co 合金化提升 ORR 活性与耐久性，在 0.9 V 下质量比活性提升 3 倍。
关键词：燃料电池、氧还原反应、铂钴合金、质量比活性、阴极。
""",
}


# --- pretty PASS/FAIL logging ----------------------------------------------

_results: list[tuple[str, bool, str]] = []


def _record(step: str, ok: bool, detail: str = "") -> bool:
    tag = "PASS" if ok else "FAIL"
    line = f"  [{tag}] {step}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)
    _results.append((step, ok, detail))
    return ok


# --- environment / safety ---------------------------------------------------


def _require_isolated_env() -> tuple[Path, Path]:
    home = os.environ.get("LOCALRAG_HOME", "")
    notes = os.environ.get("LOCALRAG_NOTES_DIR", "")
    if not home or not notes:
        sys.exit(
            "REFUSING TO RUN: set LOCALRAG_HOME and LOCALRAG_NOTES_DIR to a "
            "throwaway temp location first (this tool builds and queries a "
            "ChromaDB and must never touch your real ~/.localrag)."
        )
    home_p = Path(home).expanduser().resolve()
    default_home = (Path.home() / ".localrag").resolve()
    if home_p == default_home:
        sys.exit(
            f"REFUSING TO RUN: LOCALRAG_HOME={home_p} is the real production "
            "library. Point it at a temp directory."
        )
    return home_p, Path(notes).expanduser().resolve()


def _resolve_spawn_python() -> str:
    """Return a NON-venv Python to spawn the server with, so the launcher's
    re-exec-into-.venv path is exercised. Precedence:
      1. $LOCALRAG_E2E_SPAWN_PYTHON (explicit override)
      2. shutil.which('python') / shutil.which('python3')
      3. sys.base_prefix fallback
    """
    venv_pythons = {
        str((REPO_ROOT / ".venv" / "Scripts" / "python.exe").resolve()).lower()
        if (REPO_ROOT / ".venv" / "Scripts" / "python.exe").exists()
        else "",
        str((REPO_ROOT / ".venv" / "bin" / "python").resolve()).lower()
        if (REPO_ROOT / ".venv" / "bin" / "python").exists()
        else "",
    }

    candidates: list[str] = []
    override = os.environ.get("LOCALRAG_E2E_SPAWN_PYTHON")
    if override:
        candidates.append(override)
    which = shutil.which("python") or shutil.which("python3")
    if which:
        candidates.append(which)
    # Last resort: the base interpreter behind any venv.
    candidates.append(str(Path(sys.base_prefix) / ("python.exe" if sys.platform == "win32" else "bin/python")))

    for c in candidates:
        if c and Path(c).exists() and str(Path(c).resolve()).lower() not in venv_pythons:
            return str(Path(c).resolve())
    # If everything resolved to the venv, fall back to it (re-exec will no-op
    # but the rest of the round-trip still validates).
    return sys.executable


# --- build step -------------------------------------------------------------


def _build_corpus(home: Path, notes_dir: Path) -> None:
    print(f"[build] resetting isolated state under {home}", flush=True)
    chroma = home / "chroma"
    if chroma.exists():
        shutil.rmtree(chroma, ignore_errors=True)
    ledger = home / "processed_notes.txt"
    if ledger.exists():
        ledger.unlink()

    notes_dir.mkdir(parents=True, exist_ok=True)
    for name, body in _CORPUS.items():
        (notes_dir / name).write_text(body, encoding="utf-8")
    print(f"[build] wrote {len(_CORPUS)} synthetic notes to {notes_dir}", flush=True)

    print("[build] running build_notes_db.py (fastembed model may download ~0.22 GB on first run)...", flush=True)
    completed = subprocess.run(
        [sys.executable, str(BUILD_NOTES_DB)],
        cwd=str(REPO_ROOT),
        env=os.environ.copy(),
    )
    if completed.returncode != 0:
        sys.exit(f"[build] build_notes_db.py failed (exit {completed.returncode})")
    print("[build] notes DB built.", flush=True)


# --- MCP round-trip ---------------------------------------------------------


def _tool_payload(result) -> dict:
    """Extract the dict a FastMCP tool returned from a CallToolResult."""
    # Prefer the raw text content (always the JSON-serialized return value).
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                pass
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        # FastMCP wraps non-object returns under "result"; unwrap if present.
        return sc.get("result", sc) if set(sc.keys()) == {"result"} else sc
    return {}


async def _run_roundtrip(spawn_python: str) -> bool:
    from mcp import ClientSession
    try:
        from mcp import StdioServerParameters
    except ImportError:  # older SDKs export it from the submodule
        from mcp.client.stdio import StdioServerParameters  # type: ignore
    from mcp.client.stdio import stdio_client

    server_env = os.environ.copy()  # carries the isolated LOCALRAG_* paths
    params = StdioServerParameters(
        command=spawn_python,
        args=[str(LAUNCHER)],
        cwd=str(REPO_ROOT),
        env=server_env,
    )

    print(f"\n[e2e] spawning server: {spawn_python} {LAUNCHER}", flush=True)
    print(f"[e2e] (client interpreter is {sys.executable})", flush=True)

    all_ok = True
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # 1. list_tools contains the four expected tools.
            tools_res = await session.list_tools()
            names = {t.name for t in tools_res.tools}
            expected = {"search_notes", "search_papers", "get_note", "index_status"}
            all_ok &= _record(
                "list_tools exposes the 4 retrieval tools",
                expected <= names,
                f"got {sorted(names)}",
            )

            # 2. index_status: notes ready, papers absent, fastembed active.
            status = _tool_payload(await session.call_tool("index_status", {}))
            notes_ready = status.get("notes_ready") is True
            papers_absent = status.get("papers_ready") is False
            active_model = str(status.get("active_embed_model", ""))
            embed_provider = (status.get("embedding") or {}).get("provider")
            all_ok &= _record(
                "index_status.notes_ready is True",
                notes_ready,
                f"note_count={status.get('note_count')}",
            )
            all_ok &= _record(
                "index_status.papers_ready is False (papers collection absent)",
                papers_absent,
                f"papers_ready={status.get('papers_ready')}",
            )
            all_ok &= _record(
                "index_status active embedding is the fastembed MiniLM model",
                embed_provider == "fastembed" and "MiniLM" in active_model,
                f"provider={embed_provider} model={active_model}",
            )

            # 3. search_notes ranks the OER note first.
            notes_payload = _tool_payload(
                await session.call_tool("search_notes", {"query": E2E_QUERY, "n": 3})
            )
            hits = notes_payload.get("results", [])
            top_source = (hits[0].get("metadata", {}).get("source_file") if hits else "")
            all_ok &= _record(
                f"search_notes({E2E_QUERY!r}) returns hits",
                bool(hits) and "error" not in notes_payload,
                f"{len(hits)} hits; payload_keys={sorted(notes_payload.keys())}",
            )
            all_ok &= _record(
                "search_notes ranks the OER note #1 (semantic discrimination)",
                top_source == E2E_EXPECT_TOP_SOURCE,
                f"top source_file={top_source!r}",
            )

            # 4. get_note returns the full document for the top hit.
            get_source = top_source or E2E_EXPECT_TOP_SOURCE
            note_payload = _tool_payload(
                await session.call_tool("get_note", {"source": get_source})
            )
            note_list = note_payload.get("notes", [])
            full = note_list[0].get("content", "") if note_list else ""
            all_ok &= _record(
                "get_note(source=...) returns full note content",
                bool(note_list) and "氧析出反应" in full and len(full) > 200,
                f"content_len={len(full)}",
            )

            # 5. search_papers degrades gracefully (structured error, no crash).
            papers_payload = _tool_payload(
                await session.call_tool("search_papers", {"query": "iridium oxide OER"})
            )
            all_ok &= _record(
                "search_papers degrades gracefully when papers collection is absent",
                isinstance(papers_payload, dict) and "error" in papers_payload,
                f"payload={papers_payload}",
            )

    return all_ok


# --- main -------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--build",
        action="store_true",
        help="(Re)build the synthetic 3-note corpus + ChromaDB before verifying.",
    )
    args = parser.parse_args()

    home, notes_dir = _require_isolated_env()
    print(f"[e2e] LOCALRAG_HOME      = {home}")
    print(f"[e2e] LOCALRAG_NOTES_DIR = {notes_dir}")
    print(f"[e2e] embed provider     = {os.environ.get('LOCALRAG_EMBED_PROVIDER', '(default)')}")

    if args.build:
        _build_corpus(home, notes_dir)

    spawn_python = _resolve_spawn_python()
    if str(Path(spawn_python).resolve()).lower() == str(Path(sys.executable).resolve()).lower():
        print(
            "[e2e] WARNING: could not find a non-venv Python; spawning with the "
            "current interpreter. The launcher re-exec path will not be exercised.",
            flush=True,
        )

    try:
        all_ok = asyncio.run(_run_roundtrip(spawn_python))
    except Exception as exc:  # a crashed server / transport error is itself a FAIL
        import traceback
        traceback.print_exc()
        _record("MCP stdio round-trip completed without transport error", False, str(exc))
        all_ok = False

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"\n[e2e] {passed}/{total} checks passed.", flush=True)
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
