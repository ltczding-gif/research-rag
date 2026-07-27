"""
Cross-cut tests for the host-agnostic sub-agent contract:
  • list_pending_subagent_runs.py — what the host queries to find work
  • zotero_batch_scanner.build_analyze_command — auto-resume wiring
  • exit-code 200 contract (the manifest-pending sentinel)

These tests exist because the contract spans three modules; if any one of
them drifts, the no-API path silently breaks for Codex / OpenClaw / generic
LLM hosts. We pin the integration points here.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import list_pending_subagent_runs as lp
import zotero_batch_scanner as zbs


# --- list_pending_subagent_runs --------------------------------------------


def _write_manifest(run_dir: Path, stage: str, expected_output_name: str) -> Path:
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / f"manifest-{stage}.json"
    expected = run_dir / expected_output_name
    payload = {
        "schema_version": 3,
        "stage": stage,
        "combined_hash": run_dir.name,
        "pdf_paths": [str(run_dir / "fake.pdf")],
        "expected_output_path": str(expected),
    }
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    return manifest_path


def test_discover_pending_returns_empty_when_no_runs(tmp_path):
    assert lp.discover_pending(tmp_path / "runs") == []


def test_discover_pending_returns_unfilled_manifests(tmp_path):
    runs_root = tmp_path / "runs"
    a = runs_root / "hashA"
    b = runs_root / "hashB"
    _write_manifest(a, "profiler", "01-document-profile.json")
    _write_manifest(b, "note_generator", "02-note-draft.json")

    pending = lp.discover_pending(runs_root)

    hashes = sorted(p["combined_hash"] for p in pending)
    assert hashes == ["hashA", "hashB"]
    stages = {p["combined_hash"]: p["stage"] for p in pending}
    assert stages == {"hashA": "profiler", "hashB": "note_generator"}


def test_discover_pending_skips_stages_with_filled_output(tmp_path):
    """If the sub-agent already wrote the expected JSON, that run is no
    longer pending — the next scanner pass will pick it up."""
    runs_root = tmp_path / "runs"
    a = runs_root / "hashA"
    _write_manifest(a, "profiler", "01-document-profile.json")
    # Sub-agent has filled in the output:
    (a / "01-document-profile.json").write_text(
        json.dumps({"recommended_template": "x"}), encoding="utf-8"
    )

    assert lp.discover_pending(runs_root) == []


def test_discover_pending_ignores_empty_output_files(tmp_path):
    """Zero-byte or whitespace-only output files do not count as filled."""
    runs_root = tmp_path / "runs"
    a = runs_root / "hashA"
    _write_manifest(a, "profiler", "01-document-profile.json")
    (a / "01-document-profile.json").write_text("   \n", encoding="utf-8")

    pending = lp.discover_pending(runs_root)
    assert len(pending) == 1


def test_discover_pending_ignores_invalid_json_output(tmp_path):
    """If the sub-agent's output isn't valid JSON, treat it as not done so
    the parent agent re-dispatches instead of advancing on garbage."""
    runs_root = tmp_path / "runs"
    a = runs_root / "hashA"
    _write_manifest(a, "profiler", "01-document-profile.json")
    (a / "01-document-profile.json").write_text("not json", encoding="utf-8")

    pending = lp.discover_pending(runs_root)
    assert len(pending) == 1


def test_discover_pending_picks_latest_manifest_when_multiple(tmp_path):
    """If both Stage A and Stage B manifests exist (Stage A already
    consumed, Stage B emitted), only the latest matters."""
    runs_root = tmp_path / "runs"
    a = runs_root / "hashA"
    _write_manifest(a, "profiler", "01-document-profile.json")
    (a / "01-document-profile.json").write_text(
        json.dumps({"x": 1}), encoding="utf-8"
    )
    _write_manifest(a, "note_generator", "02-note-draft.json")

    pending = lp.discover_pending(runs_root)
    assert len(pending) == 1
    assert pending[0]["stage"] == "note_generator"


def test_main_exit_code_zero_when_empty(tmp_path, capsys):
    rc = lp.main(["--runs-dir", str(tmp_path / "nonexistent")])
    assert rc == 0


def test_main_exit_code_200_when_pending(tmp_path):
    runs_root = tmp_path / "runs"
    a = runs_root / "hashA"
    _write_manifest(a, "profiler", "01-document-profile.json")

    rc = lp.main(["--runs-dir", str(runs_root), "--quiet"])
    assert rc == lp.SUBAGENT_PENDING_EXIT_CODE == 200


def test_main_json_output_is_parseable(tmp_path, capsys):
    runs_root = tmp_path / "runs"
    a = runs_root / "hashA"
    _write_manifest(a, "profiler", "01-document-profile.json")

    rc = lp.main(["--runs-dir", str(runs_root), "--json"])
    assert rc == 200

    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    entry = parsed[0]
    # Host platforms rely on these field names; pin them.
    for key in (
        "run_dir",
        "stage",
        "manifest_path",
        "expected_output_path",
        "combined_hash",
        "pdf_paths",
        "source_artifacts",
    ):
        assert key in entry, f"missing field {key!r} in JSON output"


# --- batch scanner: auto-resume wiring -------------------------------------


def _make_args(**overrides):
    """Build a minimal Namespace satisfying build_analyze_command."""
    base = dict(
        force=False,
        out_dir=None,
        gcs_bucket=None,
        backend="subagent",
        model_router=None,
        routing_policy=None,
        model=None,
        flash_model=None,
        pro_model=None,
        publish_target="vault",
        post_publish=None,
        note_index_file=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_build_analyze_command_appends_resume_when_run_dir_exists(tmp_path, monkeypatch):
    """When --backend subagent and a deterministic run_dir already exists
    for the group, the batch wrapper must add --resume <run_dir> so the
    next pass advances stages instead of overwriting Stage A."""
    fake_pdf = tmp_path / "paper.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 test")
    group = (str(fake_pdf),)

    # Redirect the runs root to a tmp area, then pre-create the run_dir
    # for this group as if a previous batch pass had emitted Stage A.
    monkeypatch.setattr(zbs, "_runs_dir", lambda: tmp_path / "runs")
    expected_run_dir = zbs._subagent_run_dir_for_group(group)
    expected_run_dir.mkdir(parents=True)

    args = _make_args(backend="subagent")
    cmd = zbs.build_analyze_command(group, args, "scanner/gemini_analyze_pdf.py")

    assert "--resume" in cmd
    resume_idx = cmd.index("--resume")
    assert cmd[resume_idx + 1] == str(expected_run_dir)


def test_build_analyze_command_omits_resume_when_run_dir_absent(tmp_path, monkeypatch):
    """First pass — no run_dir yet — must not pass --resume."""
    fake_pdf = tmp_path / "paper.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 test")
    group = (str(fake_pdf),)

    monkeypatch.setattr(zbs, "_runs_dir", lambda: tmp_path / "runs")

    args = _make_args(backend="subagent")
    cmd = zbs.build_analyze_command(group, args, "scanner/gemini_analyze_pdf.py")

    assert "--resume" not in cmd


def test_build_analyze_command_never_resumes_for_non_subagent_backend(tmp_path, monkeypatch):
    """Even if a run dir from a prior subagent run sits on disk, switching
    to a non-subagent backend must not trigger --resume (the flag is
    sub-agent-specific and gemini_analyze_pdf will warn otherwise)."""
    fake_pdf = tmp_path / "paper.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 test")
    group = (str(fake_pdf),)

    monkeypatch.setattr(zbs, "_runs_dir", lambda: tmp_path / "runs")
    expected_run_dir = zbs._subagent_run_dir_for_group(group)
    expected_run_dir.mkdir(parents=True)

    args = _make_args(backend="vertex")
    cmd = zbs.build_analyze_command(group, args, "scanner/gemini_analyze_pdf.py")

    assert "--resume" not in cmd


# --- exit code constants stay in sync --------------------------------------


def test_pending_exit_code_constants_agree():
    """The 200 sentinel is referenced in three places (gemini_analyze_pdf,
    batch scanner, helper). They must agree, otherwise the host's guard
    loop misclassifies states. We also pin the literal in
    gemini_analyze_pdf.py — no module-level constant there yet, so we
    grep the source file directly to catch a future drift."""
    assert zbs.SUBAGENT_PENDING_EXIT_CODE == 200
    assert lp.SUBAGENT_PENDING_EXIT_CODE == 200

    src = (Path(zbs.__file__).resolve().parent / "gemini_analyze_pdf.py").read_text(
        encoding="utf-8"
    )
    assert "sys.exit(200)" in src, (
        "gemini_analyze_pdf.py must exit 200 on SubagentManifestPending; "
        "if you refactored it, update this test and ensure the contract holds."
    )


# --- helper: invalid encodings should not crash the resume loop -------------


def test_is_output_filled_handles_invalid_utf8(tmp_path):
    """A sub-agent that crashed mid-write may leave an invalid UTF-8
    multi-byte sequence on disk. The helper must treat that as
    not-filled, not raise UnicodeDecodeError."""
    bad = tmp_path / "01-document-profile.json"
    # Truncated multi-byte UTF-8 (start of a 3-byte sequence, no continuation):
    bad.write_bytes(b"\xe4\xb8")

    assert lp._is_output_filled(bad) is False


def test_is_output_filled_handles_invalid_utf8_in_subagent(tmp_path):
    """Same invariant but for the resume-path mirror in subagent.py.
    These two functions MUST stay in lockstep."""
    from backends.subagent import _is_output_filled as backend_is_filled

    bad = tmp_path / "01-document-profile.json"
    bad.write_bytes(b"\xe4\xb8")

    assert backend_is_filled(bad) is False


# --- batch summary delegates to discover_pending instead of alpha-sort ------


def test_batch_summary_picks_correct_stage_when_both_manifests_exist(
    tmp_path, monkeypatch, capsys
):
    """Regression test for a bug where _print_pending_subagent_summary
    used `sorted(run_dir.glob("manifest-*.json"))[-1]` and picked
    manifest-profiler.json (alphabetically last) even when Stage A was
    already filled and Stage B was the actual pending one."""
    runs_root = tmp_path / "runs"
    a = runs_root / "hashA"
    a.mkdir(parents=True)

    # Stage A: manifest emitted, output filled.
    (a / "manifest-profiler.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "stage": "profiler",
                "combined_hash": "hashA",
                "pdf_paths": [str(tmp_path / "p.pdf")],
                "expected_output_path": str(a / "01-document-profile.json"),
            }
        ),
        encoding="utf-8",
    )
    (a / "01-document-profile.json").write_text(
        json.dumps({"recommended_template": "x"}), encoding="utf-8"
    )

    # Stage B: manifest emitted, output NOT filled — this is the truly pending one.
    (a / "manifest-note_generator.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "stage": "note_generator",
                "combined_hash": "hashA",
                "pdf_paths": [str(tmp_path / "p.pdf")],
                "expected_output_path": str(a / "02-note-draft.json"),
            }
        ),
        encoding="utf-8",
    )

    # Make build_analyze_command's run_dir resolution land on our fake.
    monkeypatch.setattr(zbs, "_runs_dir", lambda: runs_root)
    # Match what _subagent_run_dir_for_group would compute for this fake group.
    fake_pdf = tmp_path / "p.pdf"
    fake_pdf.write_bytes(b"%PDF-1.4 test")
    group = (str(fake_pdf),)
    monkeypatch.setattr(
        zbs, "_subagent_run_dir_for_group", lambda g, _a=a: a if g == group else _a
    )

    zbs._print_pending_subagent_summary([group], _make_args())

    captured = capsys.readouterr().out
    # The summary must point at the unfilled (Stage B) manifest, not the
    # already-completed Stage A.
    assert "manifest-note_generator.json" in captured
    assert "02-note-draft.json" in captured
    # (manifest-profiler.json may still appear in run_dir paths printed
    # earlier in the summary header; the per-entry lines must NOT reference
    # the profiler manifest as the next thing to do.)
    profiler_lines = [
        line for line in captured.splitlines()
        if "manifest:" in line and "profiler" in line
    ]
    assert profiler_lines == [], (
        f"Pending summary still surfaces the already-completed "
        f"profiler manifest as next work: {profiler_lines}"
    )
