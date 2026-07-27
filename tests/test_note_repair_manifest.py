import hashlib
import json
from pathlib import Path

from benchmarks.scripts.build_note_repair_manifest import build_repair_manifest


def test_build_repair_manifest_is_narrow_and_provenance_preserving(tmp_path: Path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF")
    candidate = run_dir / "02-note-draft.json"
    candidate.write_text('{"frontmatter": {}, "body_markdown": "draft"}', encoding="utf-8")
    rendered = run_dir / "04-rendered-note.md"
    rendered.write_text("rendered", encoding="utf-8")
    brief = run_dir / "06-cross-audit-repair-brief.md"
    brief.write_text("Fix the denominator using Main p.9.", encoding="utf-8")
    generation = {
        "combined_hash": "a" * 64,
        "pdf_paths": [str(pdf)],
        "system_prompt": "system",
        "user_prompt": "user",
        "response_schema": {"type": "OBJECT"},
    }
    (run_dir / "manifest-note_generator.json").write_text(
        json.dumps(generation), encoding="utf-8"
    )

    manifest_path = build_repair_manifest(
        run_dir,
        brief,
        model_hint="test-model",
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert payload["stage"] == "note_repair"
    assert payload["model_hint"] == "test-model"
    assert payload["source_candidate_path"] == str(candidate)
    assert payload["source_candidate_sha256"] == hashlib.sha256(
        candidate.read_bytes()
    ).hexdigest()
    assert payload["repair_brief_sha256"] == hashlib.sha256(
        brief.read_bytes()
    ).hexdigest()
    assert payload["expected_output_path"].endswith("02-note-draft.repaired.json")
    assert "overwrite source_candidate_path" in " ".join(
        payload["subagent_task"]["must_not"]
    ).lower()
    assert "Independent cross-audit repair brief" in payload["user_prompt"]
