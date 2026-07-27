from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest
import yaml

from _hashing import stable_combined_hash
from benchmarks.overnight import (
    BlockedTaskError,
    OvernightRunner,
    RunStore,
    TaskStatus,
)
from benchmarks.researchqa_live import (
    create_adapter,
    normalize_paper_id,
    prepare_rq2_corpus,
)


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
    assert all(len(job["source_artifacts"]) == 1 for job in jobs)
    jobs_by_paper = {job["paper_id"]: job for job in jobs}
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


@pytest.mark.parametrize("command", ["canary", "run"])
def test_adapter_unconnected_live_commands_block_instead_of_succeeding(
    tmp_path,
    command,
):
    config, _ = _fixture_cache(tmp_path)
    run_root = tmp_path / command
    adapter = create_adapter(config, run_root)
    store = RunStore(run_root)
    state = store.create(
        run_id=f"fixture-{command}",
        fingerprints={"fixture": "v1"},
        budget_seconds=60,
    )
    runner = OvernightRunner(
        state=state,
        store=store,
        retry_delays=(),
    )
    spec = tuple(adapter.task_specs(command, state))[0]

    with pytest.raises(BlockedTaskError, match="not connected"):
        runner.run_task(spec, adapter.run_task)

    task = state.tasks[spec.task_id(state.run_id)]
    assert task.status is TaskStatus.BLOCKED
    assert not task.artifacts


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
