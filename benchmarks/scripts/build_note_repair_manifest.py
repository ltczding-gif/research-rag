#!/usr/bin/env python3
"""Build a provenance-preserving manifest for an audited note repair pass."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_repair_manifest(
    run_dir: Path,
    brief_path: Path,
    *,
    model_hint: str,
) -> Path:
    run_dir = run_dir.resolve()
    brief_path = brief_path.resolve()
    generation_manifest_path = run_dir / "manifest-note_generator.json"
    candidate_path = run_dir / "02-note-draft.json"
    rendered_path = run_dir / "04-rendered-note.md"

    for required in (
        generation_manifest_path,
        candidate_path,
        rendered_path,
        brief_path,
    ):
        if not required.is_file():
            raise FileNotFoundError(required)

    generation = _read_json(generation_manifest_path)
    brief = brief_path.read_text(encoding="utf-8").strip()
    if not brief:
        raise ValueError("repair brief must not be empty")

    expected_output_path = run_dir / "02-note-draft.repaired.json"
    manifest_path = run_dir / "manifest-note_repair.json"
    repair_user_prompt = (
        generation["user_prompt"]
        + "\n\n## Independent cross-audit repair brief\n"
        + brief
        + "\n\nRepair only source-verifiable issues in this brief. Preserve correct "
        "content and the response schema. Re-read the cited Main/SI physical "
        "pages before changing any factual statement."
    )

    manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stage": "note_repair",
        "model_hint": model_hint,
        "temperature": 0.0,
        "combined_hash": generation["combined_hash"],
        "pdf_paths": generation["pdf_paths"],
        "system_prompt": generation["system_prompt"],
        "user_prompt": repair_user_prompt,
        "response_schema": generation["response_schema"],
        "source_generation_manifest_path": str(generation_manifest_path),
        "source_candidate_path": str(candidate_path),
        "source_candidate_sha256": _sha256_file(candidate_path),
        "source_rendered_note_path": str(rendered_path),
        "repair_brief_path": str(brief_path),
        "repair_brief_sha256": _sha256_file(brief_path),
        "expected_output_path": str(expected_output_path),
        "run_dir": str(run_dir),
        "subagent_task": {
            "role": (
                "Fresh independent repair sub-agent. It did not generate or "
                "audit the candidate."
            ),
            "steps": [
                "Read every PDF listed in pdf_paths.",
                "Read source_candidate_path, source_rendered_note_path, and repair_brief_path.",
                "Apply system_prompt + user_prompt; make only source-verifiable repairs requested by the brief.",
                "Produce one JSON object that strictly conforms to response_schema.",
                "Write it atomically only to expected_output_path.",
                "Stop without invoking the scanner or touching any other file.",
            ],
            "must_not": [
                "Re-invoke the scanner.",
                "Touch the ledger.",
                "Overwrite source_candidate_path or source_rendered_note_path.",
                "Read documents beyond pdf_paths.",
                "Introduce facts from external search or machine-global skills.",
            ],
        },
        "parent_agent_task": {
            "role": "Validate and promote the repaired JSON, then resume rendering.",
            "steps": [
                "Validate expected_output_path against response_schema.",
                "Quarantine the pre-repair candidate instead of deleting it.",
                "Move expected_output_path to source_candidate_path.",
                "Run the generation manifest's parent_agent_task.resume_command.",
                "Repeat independent cross-audit before freezing fixtures.",
            ],
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
        newline="\n",
    )
    return manifest_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--brief", type=Path, required=True)
    parser.add_argument("--model", default="gpt-5.6-sol")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    manifest_path = build_repair_manifest(
        args.run_dir,
        args.brief,
        model_hint=args.model,
    )
    print(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
