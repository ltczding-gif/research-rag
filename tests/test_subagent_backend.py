"""Smoke tests for scanner/backends/subagent.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backends import SubagentManifestPending, make_backend


@pytest.fixture
def fake_pdf(tmp_path: Path) -> Path:
    p = tmp_path / "paper.pdf"
    p.write_bytes(b"%PDF-1.4 not actually a pdf, for tests")
    return p


def _profiler_kwargs(schema=None):
    return dict(
        stage="profiler",
        system_prompt="profiler system",
        user_prompt="profiler user",
        schema=schema or {"type": "object", "properties": {"recommended_template": {"type": "string"}}},
        model_id="flash",
        temperature=0.0,
    )


def test_first_call_writes_manifest_and_raises(tmp_path, fake_pdf):
    run_dir = tmp_path / "run"
    backend = make_backend("subagent", run_dir_provider=lambda: run_dir)
    backend.attach_pdfs([fake_pdf], combined_hash="testhash")

    with pytest.raises(SubagentManifestPending) as exc_info:
        backend.call_model(**_profiler_kwargs())

    manifest = exc_info.value.manifest_path
    assert manifest.exists()
    assert manifest.name == "manifest-profiler.json"

    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["stage"] == "profiler"
    assert payload["combined_hash"] == "testhash"
    assert str(fake_pdf.resolve()) in payload["pdf_paths"]
    assert payload["system_prompt"] == "profiler system"
    assert payload["user_prompt"] == "profiler user"
    assert payload["expected_output_path"].endswith("01-document-profile.json")
    # Manifest must include a forward reference so the user knows what
    # to do next.
    assert "next_step" in payload

    # schema_version 3: two-actor protocol must be present and well-formed.
    # The host contract (skills/.../references/subagent-host-contract.md)
    # promises these to host platforms; tests pin the shape so we can't
    # silently drop them.
    assert payload["schema_version"] == 3
    assert "run_dir" in payload
    sub_task = payload["subagent_task"]
    assert "role" in sub_task
    assert isinstance(sub_task["steps"], list) and len(sub_task["steps"]) >= 3
    assert isinstance(sub_task["must_not"], list) and len(sub_task["must_not"]) >= 1
    parent_task = payload["parent_agent_task"]
    assert "role" in parent_task
    assert isinstance(parent_task["steps"], list) and len(parent_task["steps"]) >= 1
    # Parent's follow-up step should reference --resume so the loop is closed.
    parent_steps_text = " ".join(parent_task["steps"])
    assert "--resume" in parent_steps_text


def test_resume_mode_returns_parsed_json(tmp_path, fake_pdf):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    # Pre-populate as if a sub-agent had already completed Stage A.
    profile = {"recommended_template": "electrocatalysis-experimental"}
    (run_dir / "01-document-profile.json").write_text(
        json.dumps(profile), encoding="utf-8"
    )

    backend = make_backend("subagent", resume_dir=run_dir)
    backend.attach_pdfs([fake_pdf], combined_hash="testhash")

    result = backend.call_model(**_profiler_kwargs())
    assert result == profile


def test_resume_mode_falls_through_to_manifest_for_missing_stage(tmp_path, fake_pdf):
    """In resume mode, if Stage A is filled but Stage B is missing, the
    next call_model should write Stage B's manifest (not raise an error)."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "01-document-profile.json").write_text(
        json.dumps({"recommended_template": "x"}), encoding="utf-8"
    )

    backend = make_backend("subagent", resume_dir=run_dir)
    backend.attach_pdfs([fake_pdf], combined_hash="testhash")

    # Stage A: returns existing profile
    profile = backend.call_model(**_profiler_kwargs())
    assert profile == {"recommended_template": "x"}

    # Stage B: no output yet, so it must write its manifest and raise
    note_kwargs = dict(
        stage="note_generator",
        system_prompt="note system",
        user_prompt="note user",
        schema={"type": "object", "properties": {"frontmatter": {"type": "object"}}},
        model_id="pro",
        temperature=0.0,
    )
    with pytest.raises(SubagentManifestPending) as exc_info:
        backend.call_model(**note_kwargs)
    assert exc_info.value.manifest_path.name == "manifest-note_generator.json"


def test_call_without_attach_pdfs_raises_runtime_error(fake_pdf):
    backend = make_backend("subagent")
    with pytest.raises(RuntimeError, match="attach_pdfs"):
        backend.call_model(**_profiler_kwargs())


# --- Stage A profiler-PDF dispatch -----------------------------------------


def test_profiler_manifest_lists_sliced_pdf_when_provided(tmp_path, fake_pdf):
    """When the orchestrator attaches both full and profiler PDFs, the
    Stage A manifest must list only the profiler (sliced) path. This is
    the hard contract — a sub-agent reading the manifest verbatim must
    not see the full PDF for classification."""
    run_dir = tmp_path / "run"
    sliced = tmp_path / "profiler_first3.pdf"
    sliced.write_bytes(b"%PDF-1.4 sliced")

    backend = make_backend("subagent", run_dir_provider=lambda: run_dir)
    backend.attach_pdfs(
        [fake_pdf],
        combined_hash="testhash",
        profiler_pdf_paths=[sliced],
    )

    with pytest.raises(SubagentManifestPending) as exc_info:
        backend.call_model(**_profiler_kwargs())

    manifest = json.loads(exc_info.value.manifest_path.read_text(encoding="utf-8"))
    assert manifest["pdf_paths"] == [str(sliced.resolve())]
    assert str(fake_pdf.resolve()) not in manifest["pdf_paths"]


def test_note_generator_manifest_lists_full_pdfs(tmp_path, fake_pdf):
    """Stage B must always see the full PDF set, regardless of whether
    a profiler-truncated set was attached."""
    run_dir = tmp_path / "run"
    sliced = tmp_path / "profiler_first3.pdf"
    sliced.write_bytes(b"%PDF-1.4 sliced")

    backend = make_backend("subagent", run_dir_provider=lambda: run_dir)
    backend.attach_pdfs(
        [fake_pdf],
        combined_hash="testhash",
        profiler_pdf_paths=[sliced],
    )

    note_kwargs = dict(
        stage="note_generator",
        system_prompt="note system",
        user_prompt="note user",
        schema={"type": "object", "properties": {"frontmatter": {"type": "object"}}},
        model_id="pro",
        temperature=0.0,
    )
    with pytest.raises(SubagentManifestPending) as exc_info:
        backend.call_model(**note_kwargs)

    manifest = json.loads(exc_info.value.manifest_path.read_text(encoding="utf-8"))
    assert manifest["pdf_paths"] == [str(fake_pdf.resolve())]
    assert str(sliced.resolve()) not in manifest["pdf_paths"]


def test_resume_falls_through_when_output_is_empty(tmp_path, fake_pdf):
    """If the sub-agent crashed mid-write and left a 0-byte output file,
    resume mode must NOT crash with a parse error. It must fall through
    to manifest re-emission so the parent agent re-dispatches."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "01-document-profile.json").write_text("", encoding="utf-8")

    backend = make_backend("subagent", resume_dir=run_dir)
    backend.attach_pdfs([fake_pdf], combined_hash="testhash")

    with pytest.raises(SubagentManifestPending):
        backend.call_model(**_profiler_kwargs())


def test_resume_falls_through_when_output_is_invalid_json(tmp_path, fake_pdf):
    """Same as above, but the output exists and is non-empty yet not
    valid JSON. Falling through to re-dispatch is friendlier than
    raising RuntimeError mid-pipeline."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "01-document-profile.json").write_text(
        "this is not json", encoding="utf-8"
    )

    backend = make_backend("subagent", resume_dir=run_dir)
    backend.attach_pdfs([fake_pdf], combined_hash="testhash")

    with pytest.raises(SubagentManifestPending):
        backend.call_model(**_profiler_kwargs())


def test_manifest_resume_command_contains_real_paths(tmp_path, fake_pdf):
    """The pre-interpolated resume_command must list the actual PDF
    paths, not the literal placeholder `<pdf_paths>`. Otherwise a
    Codex-style sub-agent might shell-exec the placeholder verbatim."""
    run_dir = tmp_path / "run"
    backend = make_backend("subagent", run_dir_provider=lambda: run_dir)
    backend.attach_pdfs([fake_pdf], combined_hash="testhash")

    with pytest.raises(SubagentManifestPending) as exc_info:
        backend.call_model(**_profiler_kwargs())

    payload = json.loads(exc_info.value.manifest_path.read_text(encoding="utf-8"))
    cmd = payload["parent_agent_task"]["resume_command"]
    assert "<pdf_paths>" not in cmd
    assert str(fake_pdf.resolve()) in cmd
    assert "--resume" in cmd
    assert "--backend subagent" in cmd


def test_attach_without_profiler_paths_is_backwards_compatible(tmp_path, fake_pdf):
    """When `profiler_pdf_paths` is not passed (the legacy 4-tests
    invariant), Stage A must continue to see the full PDF set."""
    run_dir = tmp_path / "run"
    backend = make_backend("subagent", run_dir_provider=lambda: run_dir)
    backend.attach_pdfs([fake_pdf], combined_hash="testhash")

    with pytest.raises(SubagentManifestPending) as exc_info:
        backend.call_model(**_profiler_kwargs())

    manifest = json.loads(exc_info.value.manifest_path.read_text(encoding="utf-8"))
    assert manifest["pdf_paths"] == [str(fake_pdf.resolve())]
