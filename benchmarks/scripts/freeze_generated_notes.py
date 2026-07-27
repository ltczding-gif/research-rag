#!/usr/bin/env python3
"""Freeze validated subagent note runs as path-safe benchmark fixtures."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_RUNS_ROOT = (
    REPO_ROOT / "benchmarks" / "artifacts" / "wave0b2" / "subagent_pipeline" / "runs"
)
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "benchmarks" / "fixtures" / "generated_notes"
CORPUS_MANIFEST = REPO_ROOT / "benchmarks" / "corpus" / "manifest.jsonl"

ABSOLUTE_PATH_RE = re.compile(r"(?im)^[A-Za-z0-9_]+_path:\s*['\"]?[A-Za-z]:[\\/]")
PDF_PATH_RE = re.compile(r"(?m)^pdf_(\d+)_path:\s*.*$")
TEMPLATE_RE = re.compile(r"(?m)^note_template:\s*['\"]?([^'\"\r\n]+)")
KNOWN_ISSUES = {
    "liu-2024-single-atom-cobalt-orr": [
        "source-conflict-jk-95.2-vs-92.2",
    ],
    "papier-2024-proteomic-cancer-risk": [
        "source-conflict-starting-sample-count",
    ],
    "cornelio-2023-ai-descartes": [],
    "dorgeist-2024-terrestrial-carbon-fluxes": [],
    "smith-2024-supply-chain-regulations": [],
}
FIELD_NEUTRAL_SCOPE_MARKER = (
    "field-neutral: active domain-pack guidance and quality rules are excluded"
)


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _prompt_sha256(manifest: dict[str, Any]) -> str:
    prompt_contract = {
        "system_prompt": manifest["system_prompt"],
        "user_prompt": manifest["user_prompt"],
        "response_schema": manifest["response_schema"],
        "temperature": manifest["temperature"],
    }
    canonical = json.dumps(
        prompt_contract,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return _sha256_bytes(canonical)


def _rule_scope(manifest: dict[str, Any], note_template: str) -> str:
    if note_template != "generic-research-note":
        return "active-domain"
    if FIELD_NEUTRAL_SCOPE_MARKER in manifest["user_prompt"]:
        return "field-neutral"
    return "legacy-domain-mixed"


def _paper_id_for_run(
    run_manifest: dict[str, Any], corpus_records: list[dict[str, Any]]
) -> str:
    normalized_paths = [str(path).replace("\\", "/") for path in run_manifest["pdf_paths"]]
    matches = [
        record["paper_id"]
        for record in corpus_records
        if any(
            f"/files/{record['paper_id']}/main.pdf" in path
            for path in normalized_paths
        )
    ]
    if len(matches) != 1:
        raise ValueError(f"could not map run to exactly one paper: {matches}")
    return matches[0]


def _artifact_paths(record: dict[str, Any]) -> list[str]:
    return [
        f"corpus/{record['main_pdf']['artifact_path']}",
        *(f"corpus/{item['artifact_path']}" for item in record["si"]),
    ]


def _sanitize_note(note: str, record: dict[str, Any]) -> str:
    artifact_paths = _artifact_paths(record)
    seen: set[int] = set()

    def replace_path(match: re.Match[str]) -> str:
        index = int(match.group(1))
        if index >= len(artifact_paths):
            raise ValueError(
                f"{record['paper_id']}: pdf_{index}_path has no corpus artifact"
            )
        seen.add(index)
        return f"pdf_{index}_artifact_path: {artifact_paths[index]}"

    sanitized = PDF_PATH_RE.sub(replace_path, note)
    expected = set(range(len(artifact_paths)))
    if seen != expected:
        raise ValueError(
            f"{record['paper_id']}: expected PDF path slots {expected}, found {seen}"
        )
    if ABSOLUTE_PATH_RE.search(sanitized):
        raise ValueError(f"{record['paper_id']}: absolute path remains in fixture")
    return sanitized


def freeze_notes(runs_root: Path, output_root: Path, domain_pack: str) -> int:
    corpus_records = _read_jsonl(CORPUS_MANIFEST)
    corpus_by_id = {record["paper_id"]: record for record in corpus_records}
    run_by_paper: dict[str, Path] = {}

    for run_dir in sorted(path for path in runs_root.iterdir() if path.is_dir()):
        manifest_path = run_dir / "manifest-note_generator.json"
        if not manifest_path.exists():
            continue
        paper_id = _paper_id_for_run(_read_json(manifest_path), corpus_records)
        if paper_id in run_by_paper:
            raise ValueError(f"duplicate validated run for {paper_id}")
        run_by_paper[paper_id] = run_dir

    missing = set(corpus_by_id) - set(run_by_paper)
    if missing:
        raise ValueError(f"missing note runs for: {sorted(missing)}")

    output_root.mkdir(parents=True, exist_ok=True)
    fixture_records: list[dict[str, Any]] = []

    for paper_id in corpus_by_id:
        run_dir = run_by_paper[paper_id]
        validation = _read_json(run_dir / "05-validation-report.json")
        if not validation.get("canary_ready") or validation.get("forbidden_hits"):
            raise ValueError(f"{paper_id}: run did not pass canary validation")

        run_manifest = _read_json(run_dir / "manifest-note_generator.json")
        repair_manifest_path = run_dir / "manifest-note_repair.json"
        repair_manifest = (
            _read_json(repair_manifest_path)
            if repair_manifest_path.exists()
            else None
        )
        candidate_path = run_dir / "02-note-draft.json"
        rendered_path = run_dir / "04-rendered-note.md"
        rendered = rendered_path.read_text(encoding="utf-8")
        sanitized = _sanitize_note(rendered, corpus_by_id[paper_id])

        template_match = TEMPLATE_RE.search(sanitized)
        if not template_match:
            raise ValueError(f"{paper_id}: note_template missing from frontmatter")
        note_template = template_match.group(1).strip()
        rule_scope = _rule_scope(run_manifest, note_template)
        known_issue_ids = list(KNOWN_ISSUES.get(paper_id, []))
        if rule_scope == "legacy-domain-mixed":
            known_issue_ids.insert(0, "domain-pack-mismatch")

        fixture_path = output_root / f"{paper_id}.md"
        fixture_path.write_text(sanitized, encoding="utf-8", newline="\n")

        fixture_record = {
            "schema_version": 1,
            "paper_id": paper_id,
            "artifact_path": f"fixtures/generated_notes/{paper_id}.md",
            "backend": "subagent",
            "model": run_manifest["model_hint"],
            "domain_pack": domain_pack,
            "note_template": note_template,
            "rule_scope": rule_scope,
            "generated_at": run_manifest["generated_at"],
            "source_run_id": run_dir.name,
            "prompt_sha256": _prompt_sha256(run_manifest),
            "candidate_json_sha256": _sha256_file(candidate_path),
            "note_sha256": _sha256_file(fixture_path),
            "includes_main_pdf": True,
            "includes_si": True,
            "human_review_status": "pending",
            "known_issue_ids": known_issue_ids,
            "promotion_status": (
                "blocked-domain-pack-mismatch"
                if "domain-pack-mismatch" in known_issue_ids
                else "eligible-for-human-review"
            ),
        }
        if repair_manifest is not None:
            fixture_record.update(
                {
                    "repair_applied": True,
                    "repair_model": repair_manifest["model_hint"],
                    "repair_prompt_sha256": _prompt_sha256(repair_manifest),
                    "repair_brief_sha256": repair_manifest["repair_brief_sha256"],
                    "pre_repair_candidate_json_sha256": repair_manifest[
                        "source_candidate_sha256"
                    ],
                }
            )
        else:
            fixture_record["repair_applied"] = False
        fixture_records.append(fixture_record)

    manifest_text = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in fixture_records
    )
    (output_root / "manifest.jsonl").write_text(
        manifest_text, encoding="utf-8", newline="\n"
    )
    return len(fixture_records)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--domain-pack", default="catalysis")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    count = freeze_notes(
        args.runs_root.resolve(),
        args.output_root.resolve(),
        args.domain_pack,
    )
    print(f"Frozen {count} generated-note fixtures in {args.output_root.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
