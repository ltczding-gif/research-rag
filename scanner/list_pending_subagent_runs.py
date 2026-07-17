#!/usr/bin/env python3
"""
List sub-agent runs that are waiting on a sub-agent to produce output.

This is the host-agnostic entry point parent agents (Claude Code, Codex,
OpenClaw, or any LLM-driven CLI) call to discover pending work without
having to scrape scanner stdout.

Output shapes
-------------

    --json   machine-parsable list, one entry per pending stage:
             [
               {
                 "run_dir": "...",
                 "stage": "profiler",
                 "manifest_path": ".../manifest-profiler.json",
                 "expected_output_path": ".../01-document-profile.json",
                 "combined_hash": "...",
                 "pdf_paths": ["..."],
               },
               ...
             ]

    default  human-readable table for terminal use.

Exit codes
----------
    0   no pending runs (host can declare the workflow done)
    200 at least one run is pending (host should dispatch sub-agents,
        then re-run the scanner)

Why both: it lets a parent agent put this in a guard-loop without
parsing JSON ("until exit 0, dispatch + re-run"), while still offering
structured data for richer dashboards.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable

# Make `from config import ...` work when invoked from any cwd.
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

from config import PIPELINE_REPORT_ROOT  # noqa: E402


SUBAGENT_PENDING_EXIT_CODE = 200


_STAGE_OUTPUT_FILE = {
    "profiler": "01-document-profile.json",
    "note_generator": "02-note-draft.json",
}


def _runs_root(override: str | None) -> Path:
    if override:
        return Path(override).expanduser().resolve()
    return PIPELINE_REPORT_ROOT / "runs"


def _stage_from_manifest_name(name: str) -> str:
    # manifest-profiler.json → profiler
    stem = Path(name).stem
    if stem.startswith("manifest-"):
        return stem[len("manifest-"):]
    return stem


def _is_output_filled(path: Path) -> bool:
    """Treat a non-empty, JSON-parseable file as filled.

    Catching UnicodeDecodeError matters: a sub-agent that crashes mid-
    stream may leave the file with mojibake or a partial multi-byte
    sequence. We must treat that as "not filled" so the parent re-
    dispatches, not as a hard error.
    """
    if not path.exists():
        return False
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if not text.strip():
        return False
    try:
        json.loads(text)
    except json.JSONDecodeError:
        return False
    return True


def discover_pending(runs_root: Path) -> list[dict]:
    """Walk runs_root and return one entry per run_dir that still needs work.

    A run_dir is "pending" if it has at least one manifest whose
    `expected_output_path` is not yet filled with valid JSON. When several
    manifests in the same run_dir are unfilled (rare — the loop normally
    keeps at most one ahead of the sub-agent at a time), we surface the
    one most recently written. We rely on mtime, not filename order,
    because alphabetical sort puts `manifest-note_generator.json` before
    `manifest-profiler.json` even though Stage B is emitted after Stage A.
    """
    if not runs_root.exists():
        return []

    pending: list[dict] = []
    for run_dir in sorted(p for p in runs_root.iterdir() if p.is_dir()):
        manifests = list(run_dir.glob("manifest-*.json"))
        if not manifests:
            continue

        unfilled = []
        for mp in manifests:
            try:
                manifest = json.loads(mp.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            stage = manifest.get("stage") or _stage_from_manifest_name(mp.name)
            expected = manifest.get("expected_output_path")
            if expected:
                expected_path = Path(expected)
            else:
                expected_path = run_dir / _STAGE_OUTPUT_FILE.get(stage, "expected-output.json")
            if _is_output_filled(expected_path):
                continue
            try:
                mtime = mp.stat().st_mtime
            except OSError:
                mtime = 0.0
            unfilled.append((mtime, mp, manifest, stage, expected_path))

        if not unfilled:
            continue

        unfilled.sort(key=lambda item: item[0])  # oldest first
        _, latest_mp, manifest, stage, expected_path = unfilled[-1]
        pending.append(
            {
                "run_dir": str(run_dir),
                "stage": stage,
                "manifest_path": str(latest_mp),
                "expected_output_path": str(expected_path),
                "combined_hash": manifest.get("combined_hash", run_dir.name),
                "pdf_paths": list(manifest.get("pdf_paths") or []),
            }
        )
    return pending


def _format_human(pending: Iterable[dict]) -> str:
    # ASCII-only output — see _print_pending_subagent_summary in
    # zotero_batch_scanner.py for the rationale (Windows cmd legacy
    # code pages mangle emoji and bullet glyphs).
    pending = list(pending)
    if not pending:
        return "No pending sub-agent runs. [DONE]"
    lines = [f"{len(pending)} pending sub-agent run(s):", ""]
    for entry in pending:
        lines.append(f"- {entry['combined_hash']} [{entry['stage']}]")
        lines.append(f"    manifest:        {entry['manifest_path']}")
        lines.append(f"    expected_output: {entry['expected_output_path']}")
        if entry["pdf_paths"]:
            lines.append(f"    pdfs:            {len(entry['pdf_paths'])} file(s)")
        lines.append("")
    lines.append("Next action: dispatch one sub-agent per manifest with:")
    lines.append("  Read the manifest at <manifest_path>. Read each PDF in")
    lines.append("  pdf_paths. Apply system_prompt + user_prompt. Produce a JSON")
    lines.append("  object that strictly conforms to response_schema. Write it")
    lines.append("  to expected_output_path. Then re-run the batch command.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="List sub-agent runs that are waiting on a sub-agent to fill output JSON.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit a JSON list (machine-readable). Default is a human-readable table.",
    )
    parser.add_argument(
        "--runs-dir",
        help="Override the runs root. Default: $PIPELINE_REPORT_ROOT/runs.",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress the human-readable table; only the exit code matters.",
    )
    args = parser.parse_args(argv)

    runs_root = _runs_root(args.runs_dir)
    pending = discover_pending(runs_root)

    if args.json:
        print(json.dumps(pending, ensure_ascii=False, indent=2))
    elif not args.quiet:
        print(_format_human(pending))

    return SUBAGENT_PENDING_EXIT_CODE if pending else 0


if __name__ == "__main__":
    sys.exit(main())
