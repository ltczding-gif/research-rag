from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from _hashing import stable_combined_hash
from benchmarks import researchqa_live
from benchmarks.overnight import (
    BlockedTaskError,
    DeterministicTaskError,
    OvernightRunner,
    RunStore,
    TaskStatus,
    TransientTaskError,
)
from benchmarks.researchqa_models import ModelTransportError
from benchmarks.researchqa_live import (
    create_adapter,
    normalize_paper_id,
    prepare_rq2_corpus,
)
from benchmarks.researchqa_notes import NoteJob, build_scanner_command


REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = REPO_ROOT / "benchmarks" / "configs" / "rq2-overnight.yaml"
REAL_AUDIT = (
    REPO_ROOT
    / "benchmarks"
    / ".cache"
    / "researchqa"
    / "pdfs"
    / "rq-2"
    / "source-set-audit.json"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
        newline="\n",
    )


def _write_pdf(path: Path) -> None:
    pypdf = pytest.importorskip("pypdf")
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=72, height=72)
    with path.open("wb") as handle:
        writer.write(handle)


def _write_docx(path: Path) -> None:
    document_xml = """\
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>File description</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>
"""
    with zipfile.ZipFile(path, "w") as package:
        package.writestr("word/document.xml", document_xml)


def _audit_file(path: Path, **extra) -> dict:
    return {
        "local_path": str(path.resolve()),
        "bytes": path.stat().st_size,
        "sha256": _sha256(path),
        **extra,
    }


def _fixture_cache(tmp_path: Path) -> tuple[dict, Path]:
    cache_root = tmp_path / "cache"
    source_root = cache_root / "pdfs" / "rq-2"
    supplementary_root = source_root / "supplementary"
    source_root.mkdir(parents=True)
    supplementary_root.mkdir(parents=True)

    main_w20 = source_root / "W20.pdf"
    main_w100 = source_root / "W100.pdf"
    description = supplementary_root / "description.docx"
    data = supplementary_root / "data.csv"
    supplementary_pdf = supplementary_root / "supplement.pdf"
    _write_pdf(main_w20)
    _write_pdf(main_w100)
    _write_pdf(supplementary_pdf)
    _write_docx(description)
    data.write_text(
        "model,score\r\nmodel-a,0.9\r\n",
        encoding="utf-8",
        newline="",
    )

    audit = {
        "schema_version": 1,
        "summary": {"papers": 2},
        "papers": [
            {
                "paper_id": "https://openalex.org/W100",
                "main_pdf": _audit_file(
                    main_w100,
                    pages=1,
                    parse_status="ok",
                ),
                "supplementary_files": [
                    _audit_file(
                        data,
                        label="Supplementary Data 1",
                        content_type="text/csv",
                        page_count=None,
                        validation="ok",
                    ),
                    _audit_file(
                        supplementary_pdf,
                        label="Supplementary Information",
                        content_type="application/pdf",
                        page_count=1,
                        validation="ok",
                    ),
                ],
            },
            {
                "paper_id": "W20",
                "main_pdf": _audit_file(
                    main_w20,
                    pages=1,
                    parse_status="ok",
                ),
                "supplementary_files": [
                    _audit_file(
                        description,
                        label="Description of Additional Supplementary Files",
                        content_type=(
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        ),
                        page_count=None,
                        validation="ok",
                    )
                ],
            },
        ],
    }
    (source_root / "source-set-audit.json").write_text(
        json.dumps(audit, indent=2),
        encoding="utf-8",
    )
    _write_jsonl(
        source_root / "download-manifest.jsonl",
        [
            {
                "paper_id": "W20",
                "local_path": str(main_w20.resolve()),
                "download_url": "https://example.test/W20.pdf",
                "bytes": main_w20.stat().st_size,
                "sha256": _sha256(main_w20),
            },
            {
                "paper_id": "https://openalex.org/W100",
                "local_path": str(main_w100.resolve()),
                "paper_s3_url": (
                    "https://assets.openpaper.ai.s3.us-east-1.amazonaws.com/"
                    "op-evals/benchmark/W100.pdf"
                ),
                "bytes": main_w100.stat().st_size,
                "sha256": _sha256(main_w100),
            },
        ],
    )
    _write_jsonl(
        supplementary_root / "download-manifest.jsonl",
        [
            {
                "paper_id": "W20",
                "local_path": str(description.resolve()),
                "source_url": "https://example.test/description.docx",
                "bytes": description.stat().st_size,
                "sha256": _sha256(description),
            },
            {
                "paper_id": "W100",
                "local_path": str(data.resolve()),
                "source_url": "https://example.test/data.csv",
                "bytes": data.stat().st_size,
                "sha256": _sha256(data),
            },
            {
                "paper_id": "W100",
                "local_path": str(supplementary_pdf.resolve()),
                "source_url": "https://example.test/supplement.pdf",
                "bytes": supplementary_pdf.stat().st_size,
                "sha256": _sha256(supplementary_pdf),
            },
        ],
    )
    config = {
        "schema_version": 1,
        "config_id": "fixture-rq2",
        "benchmark": {"tier_id": "rq-2", "paper_count": 2},
        "paths": {
            "cache_root": str(cache_root.resolve()),
            "source_dir": "pdfs/rq-2",
        },
    }
    return config, source_root


def _read_jsonl(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line
    ]


def _write_canary_pass(
    run_root: Path,
    *,
    bad_note_sha: bool = False,
) -> Path:
    artifact_root = run_root / "note-runs" / "canary" / "W2792307011"
    artifact_root.mkdir(parents=True, exist_ok=True)
    note_path = artifact_root / "04-rendered-note.md"
    draft_path = artifact_root / "02-note-draft.json"
    audit_path = artifact_root / "06-audit.json"
    note_path.write_text("Audited canary note [Main p.1]\n", encoding="utf-8")
    draft_path.write_text(
        json.dumps({"frontmatter": {}, "sections": []}),
        encoding="utf-8",
    )
    note_sha256 = _sha256(note_path)
    draft_sha256 = _sha256(draft_path)
    audit_path.write_text(
        json.dumps(
            {
                "paper_id": "W2792307011",
                "verdict": "PASS",
                "p0": 0,
                "p1": 0,
                "note_sha256": note_sha256,
                "draft_sha256": draft_sha256,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    marker_path = run_root / "note-runs" / "canary-pass.json"
    marker_path.write_text(
        json.dumps(
            {
                "paper_id": "W2792307011",
                "verdict": "PASS",
                "p0": 0,
                "p1": 0,
                "note_path": str(note_path.relative_to(run_root)),
                "note_sha256": "0" * 64 if bad_note_sha else note_sha256,
                "draft_path": str(draft_path.relative_to(run_root)),
                "draft_sha256": draft_sha256,
                "audit_path": str(audit_path.relative_to(run_root)),
                "audit_sha256": _sha256(audit_path),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return marker_path


def _write_frozen_notes(run_root: Path) -> Path:
    frozen_root = run_root / "note-runs" / "frozen"
    notes_root = frozen_root / "notes"
    notes_root.mkdir(parents=True, exist_ok=True)
    rows = []
    for index in range(1, 21):
        paper_id = f"W{1000 + index}"
        note_path = notes_root / f"{paper_id}.md"
        note_path.write_text(
            f"# {paper_id}\n\nFrozen note {index}.\n",
            encoding="utf-8",
        )
        rows.append(
            {
                "schema_version": 1,
                "paper_id": paper_id,
                "template": "generic-research-note",
                "note_sha256": _sha256(note_path),
            }
        )
    manifest_path = frozen_root / "frozen-notes.jsonl"
    _write_jsonl(manifest_path, rows)
    return manifest_path


def _mutate_frozen_note_and_manifest(run_root: Path) -> None:
    manifest_path = (
        run_root / "note-runs" / "frozen" / "frozen-notes.jsonl"
    )
    rows = _read_jsonl(manifest_path)
    note_path = (
        manifest_path.parent / "notes" / f"{rows[0]['paper_id']}.md"
    )
    note_path.write_text("# changed\n\nNew frozen bytes.\n", encoding="utf-8")
    rows[0]["note_sha256"] = _sha256(note_path)
    _write_jsonl(manifest_path, rows)


def _fake_runtime_result(run_root: Path):
    runtime_root = run_root / "runtime"
    final_root = run_root / "sweep" / "final"
    raw_root = run_root / "sweep" / "raw-results"
    report_root = run_root / "report"
    runtime_root.mkdir(parents=True, exist_ok=True)
    final_root.mkdir(parents=True, exist_ok=True)
    raw_root.mkdir(parents=True, exist_ok=True)
    report_root.mkdir(parents=True, exist_ok=True)

    preflight_path = runtime_root / "model-preflight.json"
    summary_path = runtime_root / "runtime-summary.json"
    leaderboard_path = final_root / "leaderboard.json"
    pareto_path = final_root / "pareto-frontier.json"
    decision_path = final_root / "decision-summary.json"
    raw_path = raw_root / "candidate.json"
    report_paths = (
        report_root / "run-manifest.json",
        report_root / "leaderboard.csv",
        report_root / "paper-domain-breakdown.csv",
        report_root / "paired-bootstrap.json",
        report_root / "blocked-and-unmapped.jsonl",
        report_root / "morning-report.md",
    )
    preflight_path.write_text(
        json.dumps({"status": "completed"}),
        encoding="utf-8",
    )
    summary_path.write_text(
        json.dumps({"status": "completed"}),
        encoding="utf-8",
    )
    leaderboard_path.write_text(
        json.dumps({"leaderboard": []}),
        encoding="utf-8",
    )
    pareto_path.write_text(
        json.dumps({"pareto_frontier": []}),
        encoding="utf-8",
    )
    decision_path.write_text(
        json.dumps({"provisional_winner": "candidate-a"}),
        encoding="utf-8",
    )
    raw_path.write_text(
        json.dumps({"candidate_id": "candidate-a"}),
        encoding="utf-8",
    )
    for path in report_paths:
        path.write_text(
            "{}" if path.suffix == ".json" else "fixture\n",
            encoding="utf-8",
        )
    return SimpleNamespace(
        model_preflight_path=str(preflight_path),
        runtime_summary_path=str(summary_path),
        sweep_result=SimpleNamespace(
            artifact_paths=(
                str(leaderboard_path),
                str(pareto_path),
                str(decision_path),
                str(raw_path),
                *(str(path) for path in report_paths),
            ),
            provisional_winner="candidate-a",
        ),
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("W123", "W123"),
        ("https://openalex.org/W123", "W123"),
        ("https://openalex.org/W123/", "W123"),
    ],
)
def test_normalize_paper_id(value, expected):
    assert normalize_paper_id(value) == expected


def test_prepare_fixture_writes_manifests_ir_packets_and_note_jobs(tmp_path):
    config, _ = _fixture_cache(tmp_path)
    run_root = tmp_path / "run"

    first = prepare_rq2_corpus(config, run_root)
    first_manifest = (
        run_root / "source" / "W100" / "source-manifest.jsonl"
    ).read_bytes()
    second = prepare_rq2_corpus(config, run_root)

    assert first == second
    assert first["paper_count"] == 2
    assert first["note_job_count"] == 2
    assert first["source_count"] == 5
    assert first["source_index_count"] == 2
    assert first["source_packet_count"] == 2
    assert first["source_role_counts"] == {
        "auxiliary_reporting_file": 1,
        "benchmark_pdf": 2,
        "external_si": 2,
    }
    assert (
        run_root / "source" / "W100" / "source-manifest.jsonl"
    ).read_bytes() == first_manifest

    w20_manifest = _read_jsonl(
        run_root / "source" / "W20" / "source-manifest.jsonl"
    )
    w100_manifest = _read_jsonl(
        run_root / "source" / "W100" / "source-manifest.jsonl"
    )
    assert [record["file_id"] for record in w20_manifest] == [
        "Main",
        "AUX-01",
    ]
    assert [record["source_role"] for record in w20_manifest] == [
        "benchmark_pdf",
        "auxiliary_reporting_file",
    ]
    assert [record["file_id"] for record in w100_manifest] == [
        "Main",
        "SI-01",
        "SI-02",
    ]
    assert w100_manifest[0]["source_url"].startswith(
        "https://s3.us-east-1.amazonaws.com/assets.openpaper.ai/"
    )
    w100_source_index = json.loads(
        (
            run_root / "source" / "W100" / "source-index.json"
        ).read_text(encoding="utf-8")
    )
    assert [
        (
            source["file_id"],
            source["original_filename"],
            source["media_type"],
            source["citation_coordinate_type"],
        )
        for source in w100_source_index["sources"]
    ] == [
        ("Main", "W100.pdf", "application/pdf", "pdf_page"),
        ("SI-01", "data.csv", "text/csv", "csv_rows_columns"),
        ("SI-02", "supplement.pdf", "application/pdf", "pdf_page"),
    ]
    assert all(
        Path(source["path"]).is_absolute()
        for source in w100_source_index["sources"]
    )
    assert all(
        not {"text", "units", "normalized_text"} & set(source)
        for source in w100_source_index["sources"]
    )
    assert str(
        (
            run_root / "source" / "W100" / "source-index.json"
        ).resolve()
    ) in first["artifact_paths"]

    w20_ir = _read_jsonl(run_root / "source" / "W20" / "native-ir.jsonl")
    w100_ir = _read_jsonl(run_root / "source" / "W100" / "native-ir.jsonl")
    assert [unit["citation"] for unit in w20_ir] == [
        "[Main p.1]",
        "[AUX-01 para.1]",
    ]
    assert [unit["citation"] for unit in w100_ir] == [
        "[Main p.1]",
        "[SI-01 rows.1-1 cols.model,score]",
        "[SI-02 p.1]",
    ]
    assert (
        run_root
        / "source"
        / "W20"
        / "packets"
        / "AUX-01-native-source.json"
    ).is_file()
    assert (
        run_root
        / "source"
        / "W100"
        / "packets"
        / "SI-01-native-source.json"
    ).is_file()

    jobs = _read_jsonl(run_root / "note-runs" / "note-jobs.jsonl")
    assert [job["paper_id"] for job in jobs] == ["W100", "W20"]
    assert [job["page_count"] for job in jobs] == [2, 1]
    assert [job["non_pdf_si_count"] for job in jobs] == [1, 0]
    assert all(len(job["source_artifacts"]) == 2 for job in jobs)
    jobs_by_paper = {job["paper_id"]: job for job in jobs}
    for job in jobs:
        assert Path(job["source_artifacts"][0]).name == "source-index.json"
        assert Path(job["source_artifacts"][1]).name.endswith(
            "-native-source.json"
        )
    scanner_command = build_scanner_command(
        NoteJob(**jobs_by_paper["W100"]),
        python_executable="python311",
        scanner_path=REPO_ROOT / "scanner" / "gemini_analyze_pdf.py",
    )
    stage_b_artifacts = [
        scanner_command[index + 1]
        for index, value in enumerate(scanner_command)
        if value == "--source-artifact"
    ]
    assert stage_b_artifacts == jobs_by_paper["W100"]["source_artifacts"]
    expected_w100_hash = stable_combined_hash(
        [
            Path(jobs_by_paper["W100"]["main_pdf"]),
            *map(Path, jobs_by_paper["W100"]["si_pdfs"]),
        ]
    )
    expected_w20_hash = stable_combined_hash(
        [Path(jobs_by_paper["W20"]["main_pdf"])]
    )
    assert Path(jobs_by_paper["W100"]["run_dir"]) == (
        run_root / "note-runs" / "pipeline" / "runs" / expected_w100_hash
    ).resolve()
    assert Path(jobs_by_paper["W20"]["run_dir"]) == (
        run_root / "note-runs" / "pipeline" / "runs" / expected_w20_hash
    ).resolve()
    assert not Path(jobs_by_paper["W100"]["run_dir"]).exists()
    assert not list(run_root.rglob("*.tmp"))


def test_prepare_fails_closed_on_audit_hash_mismatch(tmp_path):
    config, source_root = _fixture_cache(tmp_path)
    audit_path = source_root / "source-set-audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["papers"][0]["main_pdf"]["sha256"] = "0" * 64
    audit_path.write_text(json.dumps(audit), encoding="utf-8")

    with pytest.raises(ValueError, match="audited SHA-256 mismatch"):
        prepare_rq2_corpus(config, tmp_path / "run")


def test_adapter_prepare_runs_under_overnight_and_registers_artifacts(tmp_path):
    config, _ = _fixture_cache(tmp_path)
    run_root = tmp_path / "run"
    adapter = create_adapter(config, run_root)
    store = RunStore(run_root)
    state = store.create(
        run_id="fixture-run",
        fingerprints={"fixture": "v1"},
        budget_seconds=60,
    )
    runner = OvernightRunner(
        state=state,
        store=store,
        retry_delays=(),
    )
    spec = tuple(adapter.task_specs("prepare", state))[0]

    task = runner.run_task(spec, adapter.run_task)

    assert task.status is TaskStatus.COMPLETED
    assert task.metadata["paper_count"] == 2
    assert task.artifacts
    assert all(store.verify_artifact(artifact) for artifact in task.artifacts)


def test_adapter_run_fingerprints_do_not_change_between_commands(tmp_path):
    config, _ = _fixture_cache(tmp_path)
    adapter = create_adapter(config, tmp_path / "run")

    assert adapter.fingerprints(command="prepare") == adapter.fingerprints(
        command="canary"
    )
    assert adapter.fingerprints(command="canary") == adapter.fingerprints(
        command="run"
    )


def test_adapter_task_fingerprints_bind_dynamic_canary_and_frozen_inputs(
    tmp_path,
):
    config, _ = _fixture_cache(tmp_path)
    run_root = tmp_path / "run"
    adapter = create_adapter(config, run_root)
    store = RunStore(run_root)
    state = store.create(
        run_id="fixture-dynamic",
        fingerprints={"fixture": "v1"},
        budget_seconds=60,
    )

    canary_missing = tuple(adapter.task_specs("canary", state))[0]
    marker_path = _write_canary_pass(run_root)
    canary_present = tuple(adapter.task_specs("canary", state))[0]
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    marker["created_by"] = "parent"
    marker_path.write_text(json.dumps(marker), encoding="utf-8")
    canary_changed = tuple(adapter.task_specs("canary", state))[0]

    run_missing = tuple(adapter.task_specs("run", state))[0]
    _write_frozen_notes(run_root)
    run_present = tuple(adapter.task_specs("run", state))[0]
    _mutate_frozen_note_and_manifest(run_root)
    run_changed = tuple(adapter.task_specs("run", state))[0]

    assert len(
        {
            canary_missing.input_fingerprint,
            canary_present.input_fingerprint,
            canary_changed.input_fingerprint,
        }
    ) == 3
    assert len(
        {
            run_missing.input_fingerprint,
            run_present.input_fingerprint,
            run_changed.input_fingerprint,
        }
    ) == 3
    assert adapter.fingerprints(command="prepare") == adapter.fingerprints(
        command="run"
    )


def test_adapter_canary_blocks_when_marker_is_missing(tmp_path):
    config, _ = _fixture_cache(tmp_path)
    run_root = tmp_path / "run"
    adapter = create_adapter(config, run_root)
    store = RunStore(run_root)
    state = store.create(
        run_id="fixture-canary-missing",
        fingerprints={"fixture": "v1"},
        budget_seconds=60,
    )
    runner = OvernightRunner(state=state, store=store, retry_delays=())
    spec = tuple(adapter.task_specs("canary", state))[0]

    with pytest.raises(BlockedTaskError, match="marker is not available"):
        runner.run_task(spec, adapter.run_task)

    task = state.tasks[spec.task_id(state.run_id)]
    assert task.status is TaskStatus.BLOCKED
    assert not task.artifacts


def test_adapter_canary_fails_closed_on_bad_artifact_sha(tmp_path):
    config, _ = _fixture_cache(tmp_path)
    run_root = tmp_path / "run"
    _write_canary_pass(run_root, bad_note_sha=True)
    adapter = create_adapter(config, run_root)
    store = RunStore(run_root)
    state = store.create(
        run_id="fixture-canary-bad-sha",
        fingerprints={"fixture": "v1"},
        budget_seconds=60,
    )
    runner = OvernightRunner(state=state, store=store, retry_delays=())
    spec = tuple(adapter.task_specs("canary", state))[0]

    with pytest.raises(DeterministicTaskError, match="SHA-256 mismatch"):
        runner.run_task(spec, adapter.run_task)

    task = state.tasks[spec.task_id(state.run_id)]
    assert task.status is TaskStatus.FAILED
    assert not task.artifacts


def test_adapter_fake_runtime_command_sequence_and_report(
    tmp_path,
    monkeypatch,
):
    config, _ = _fixture_cache(tmp_path)
    run_root = tmp_path / "run"
    adapter = create_adapter(config, run_root)
    store = RunStore(run_root)
    state = store.create(
        run_id="fixture-command-sequence",
        fingerprints={"fixture": "v1"},
        budget_seconds=60,
    )
    runner = OvernightRunner(state=state, store=store, retry_delays=())
    calls = []

    prepare_spec = tuple(adapter.task_specs("prepare", state))[0]
    prepare_task = runner.run_task(prepare_spec, adapter.run_task)

    _write_canary_pass(run_root)
    canary_spec = tuple(adapter.task_specs("canary", state))[0]
    canary_task = runner.run_task(canary_spec, adapter.run_task)

    _write_frozen_notes(run_root)

    def fake_runtime(runtime_config, runtime_run_root):
        calls.append((runtime_config, Path(runtime_run_root)))
        return _fake_runtime_result(Path(runtime_run_root))

    monkeypatch.setattr(
        researchqa_live.researchqa_runtime,
        "run_researchqa_runtime",
        fake_runtime,
    )
    run_spec = tuple(adapter.task_specs("run", state))[0]
    run_task = runner.run_task(run_spec, adapter.run_task)
    report_artifacts = adapter.write_report(state, store)

    assert prepare_task.status is TaskStatus.COMPLETED
    assert canary_task.status is TaskStatus.COMPLETED
    assert run_task.status is TaskStatus.COMPLETED
    assert len(canary_task.artifacts) == 4
    assert {artifact.path for artifact in report_artifacts} == {
        "sweep/final/leaderboard.json",
        "sweep/final/pareto-frontier.json",
        "sweep/final/decision-summary.json",
        "report/run-manifest.json",
        "report/leaderboard.csv",
        "report/paper-domain-breakdown.csv",
        "report/paired-bootstrap.json",
        "report/blocked-and-unmapped.jsonl",
        "report/morning-report.md",
        "runtime/runtime-summary.json",
    }
    assert {
        artifact.path for artifact in run_task.artifacts
    }.issuperset(
        {
            "runtime/model-preflight.json",
            "runtime/runtime-summary.json",
            "sweep/final/leaderboard.json",
            "sweep/final/pareto-frontier.json",
            "sweep/final/decision-summary.json",
            "sweep/raw-results/candidate.json",
            "report/run-manifest.json",
            "report/leaderboard.csv",
            "report/paper-domain-breakdown.csv",
            "report/paired-bootstrap.json",
            "report/blocked-and-unmapped.jsonl",
            "report/morning-report.md",
        }
    )
    report_media_types = {
        artifact.path: artifact.media_type for artifact in report_artifacts
    }
    assert report_media_types["report/run-manifest.json"] == "application/json"
    assert report_media_types["report/leaderboard.csv"] == "text/csv"
    assert (
        report_media_types["report/paper-domain-breakdown.csv"] == "text/csv"
    )
    assert (
        report_media_types["report/blocked-and-unmapped.jsonl"]
        == "application/x-ndjson"
    )
    assert report_media_types["report/morning-report.md"] == "text/markdown"

    runtime_media_types = {
        artifact.path: artifact.media_type for artifact in run_task.artifacts
    }
    assert runtime_media_types["report/leaderboard.csv"] == "text/csv"
    assert runtime_media_types["report/morning-report.md"] == "text/markdown"
    assert all(store.verify_artifact(artifact) for artifact in run_task.artifacts)
    assert calls == [(config, run_root.resolve())]


def test_adapter_retries_runtime_model_transport_failure(tmp_path, monkeypatch):
    config, _ = _fixture_cache(tmp_path)
    run_root = tmp_path / "run"
    _write_frozen_notes(run_root)
    adapter = create_adapter(config, run_root)
    store = RunStore(run_root)
    state = store.create(
        run_id="fixture-runtime-transport",
        fingerprints={"fixture": "v1"},
        budget_seconds=60,
    )
    runner = OvernightRunner(state=state, store=store, retry_delays=())

    def fake_runtime(_config, _run_root):
        transport = ModelTransportError("Ollama timed out")
        raise researchqa_live.researchqa_runtime.ResearchQARuntimeError(
            "runtime failed"
        ) from transport

    monkeypatch.setattr(
        researchqa_live.researchqa_runtime,
        "run_researchqa_runtime",
        fake_runtime,
    )
    spec = tuple(adapter.task_specs("run", state))[0]

    with pytest.raises(TransientTaskError, match="runtime failed"):
        runner.run_task(spec, adapter.run_task)

    task = state.tasks[spec.task_id(state.run_id)]
    assert task.status is TaskStatus.FAILED


@pytest.mark.skipif(
    not REAL_AUDIT.is_file(),
    reason="ignored rq-2 live cache is not available",
)
def test_real_rq2_cache_prepare_smoke(tmp_path):
    config = yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8"))

    summary = prepare_rq2_corpus(config, tmp_path / "real-run")

    assert summary["paper_count"] == 20
    assert summary["note_job_count"] == 20
    assert summary["source_role_counts"] == {
        "auxiliary_reporting_file": 1,
        "benchmark_pdf": 20,
        "external_si": 11,
    }
    assert summary["media_type_counts"][
        "application/vnd.openxmlformats-officedocument."
        "wordprocessingml.document"
    ] == 2
    assert summary["media_type_counts"][
        "application/vnd.openxmlformats-officedocument."
        "spreadsheetml.sheet"
    ] == 2
    assert summary["media_type_counts"]["text/csv"] == 1
    assert summary["source_packet_count"] == 5
    assert summary["source_index_count"] == 20
