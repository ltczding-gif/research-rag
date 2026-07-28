from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

from benchmarks.overnight import fingerprint_payload
from benchmarks.public_report import validate_rq2_public_manifest
from benchmarks import researchqa_public_export as public_export


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )


def _write_csv(path: Path, fields, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _fixture_run(tmp_path: Path) -> Path:
    run_root = tmp_path / "cache" / "runs" / "rq2-fixture"
    paper_ids = [f"W{index:010d}" for index in range(20)]
    question_ids = [f"q{index:03d}" for index in range(254)]
    coverage = {
        "passed": True,
        "overall": 1.0,
        "mapped_groups": 380,
        "total_groups": 380,
        "per_paper": {paper_id: 1.0 for paper_id in paper_ids},
    }
    stage_counts = public_export.EXPECTED_STAGE_COUNTS
    candidates = []
    for stage_id, count in stage_counts.items():
        for index in range(count):
            config_id = f"{stage_id}-{index:02d}"
            failed = stage_id == "pdf-chunker" and index == count - 1
            guardrails_passed = not (
                stage_id == "retriever" and index == count - 1
            )
            payload = (
                {
                    "candidate": {
                        "config_id": config_id,
                        "stage_id": stage_id,
                        "rankable": True,
                    },
                    "execution_complete": False,
                    "failure_kind": "strategy",
                    "failure_context": {
                        "phase": "candidate-execution",
                        "row_id": None,
                        "pass_index": None,
                        "progress": {
                            "completed_paper_ids": [],
                            "completed_question_ids": [],
                        },
                    },
                    "guardrail_finalized": False,
                    "error_type": "StrategyContractError",
                    "error": r"F:\private\paper.pdf",
                    "traceback": "fixture traceback",
                }
                if failed
                else {
                    "candidate": {
                        "config_id": config_id,
                        "stage_id": stage_id,
                        "rankable": not (
                            stage_id == "note-chunker" and index == 0
                        ),
                    },
                    "completed_paper_ids": paper_ids,
                    "completed_question_ids": question_ids,
                    "mapping": {"coverage": coverage},
                    "execution_complete": True,
                    "guardrail_finalized": True,
                    "guardrails_passed": guardrails_passed,
                    "retrieval_scope": "paper-scoped",
                }
            )
            envelope = {
                "schema_version": 1,
                "engine_revision": "fixture",
                "config_id": config_id,
                "stage_id": stage_id,
                "input_fingerprint": "fixture",
                "status": "failed" if failed else "completed",
                "payload_sha256": fingerprint_payload(payload),
                "payload": payload,
            }
            _write_json(
                run_root
                / "sweep"
                / "candidates"
                / stage_id
                / f"{config_id}.json",
                envelope,
            )
            candidates.append((stage_id, config_id, failed))

    winner = "top2-confirmation-00"
    baseline = "pdf-chunker-00"
    _write_json(
        run_root / "sweep" / "final" / "decision-summary.json",
        {"provisional_winner": winner},
    )
    _write_json(
        run_root / "sweep" / "final" / "pareto-frontier.json",
        {
            "schema_version": 1,
            "rows": [
                {
                    "rank": 1,
                    "config_id": winner,
                    "stage_id": "top2-confirmation",
                    "primary": 0.84,
                    "p95_latency_ms": 100.0,
                    "index_bytes": 1000,
                    "chunk_count": 100,
                    "status": "completed",
                    "guardrails_passed": True,
                    "candidate": {"private": "dropped"},
                }
            ],
        },
    )
    _write_json(
        run_root / "report" / "paired-bootstrap.json",
        {
            "schema_version": 1,
            "metric": "coverage_ndcg_at_10",
            "candidate_config_id": winner,
            "baseline_config_id": baseline,
            "observed_delta": 0.01,
            "confidence_interval": [-0.01, 0.03],
            "confidence": 0.95,
            "samples": 10_000,
            "seed": "research-rag-rq2-bootstrap-v1",
        },
    )
    leaderboard_rows = [
        {
            field: (
                stage_id
                if field == "stage_id"
                else config_id
                if field == "config_id"
                else "failed"
                if field == "status" and failed
                else "completed"
                if field == "status"
                else ""
            )
            for field in public_export.LEADERBOARD_FIELDS
        }
        for stage_id, config_id, failed in candidates
    ]
    _write_csv(
        run_root / "report" / "leaderboard.csv",
        public_export.LEADERBOARD_FIELDS,
        leaderboard_rows,
    )
    breakdown_fields = (*public_export.BREAKDOWN_BASE_FIELDS, "recall_at_5")
    _write_csv(
        run_root / "report" / "paper-domain-breakdown.csv",
        breakdown_fields,
        [
            {
                "role": "winner",
                "config_id": winner,
                "scope": "domain",
                "key": "biology",
                "domain": "biology",
                "recall_at_5": "0.9",
            }
        ],
    )
    blocked = {
        "kind": "candidate",
        "stage_id": "pdf-chunker",
        "config_id": "pdf-chunker-06",
        "status": "failed",
        "error": r"F:\private\paper.pdf",
        "rankable": True,
        "mapping_passed": False,
        "guardrails_passed": False,
    }
    (run_root / "report" / "blocked-and-unmapped.jsonl").write_text(
        json.dumps(blocked) + "\n",
        encoding="utf-8",
    )

    for paper_id in paper_ids:
        source_manifest = run_root / "source" / paper_id / "source-manifest.jsonl"
        source_manifest.parent.mkdir(parents=True, exist_ok=True)
        source_manifest.write_text('{"source":"fixture"}\n', encoding="utf-8")
    suite = run_root.parent.parent / "suites" / "rq-2" / "questions.jsonl"
    suite.parent.mkdir(parents=True, exist_ok=True)
    suite.write_text('{"row_id":"fixture"}\n', encoding="utf-8")

    frozen_root = run_root / "note-runs" / "frozen"
    frozen_rows = []
    for paper_id in paper_ids:
        note = frozen_root / "notes" / f"{paper_id}.md"
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(f"# {paper_id}\n", encoding="utf-8")
        from benchmarks.overnight import sha256_path

        _size, digest = sha256_path(note)
        frozen_rows.append({"paper_id": paper_id, "note_sha256": digest})
    (frozen_root / "frozen-notes.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in frozen_rows),
        encoding="utf-8",
    )
    _write_json(
        run_root / "runtime" / "hardware-observations.json",
        {
            "max_gpu_temperature_c": 86,
            "target_temperature_c": 87,
            "software_thermal_slowdown_observed": True,
            "hardware_thermal_slowdown_observed": False,
        },
    )

    config = yaml.safe_load(public_export.DEFAULT_CONFIG.read_text(encoding="utf-8"))
    _write_json(
        run_root / "run-state.json",
        {
            "run_id": "rq2-fixture",
            "status": "completed",
            "created_at": "2026-07-28T00:00:00Z",
            "updated_at": "2026-07-28T12:00:00Z",
            "budget_seconds": 36_000,
            "elapsed_seconds": 40_000,
            "fingerprints": {
                "code": "a" * 64,
                "config": fingerprint_payload(config),
                "embedding-model": "b" * 64,
                "reranker-model": "c" * 40,
            },
            "tasks": {
                "runtime-fixture": {
                    "status": "completed",
                }
            },
        },
    )
    return run_root


def test_rq2_public_export_is_allowlisted_valid_and_replaceable(
    tmp_path,
    monkeypatch,
):
    run_root = _fixture_run(tmp_path)
    output = tmp_path / "public" / "researchqa-rq2"
    monkeypatch.setattr(
        public_export,
        "_hardware_fingerprints",
        lambda: {
            "platform": "d" * 64,
            "cpu": "e" * 64,
            "gpu": "f" * 64,
        },
    )

    result = public_export.export_rq2_public_report(
        run_root,
        output_root=output,
    )
    public_export.export_rq2_public_report(run_root, output_root=output)

    assert result == output.resolve()
    assert {path.name for path in output.iterdir()} == {
        "morning-report.md",
        "leaderboard.csv",
        "paper-domain-breakdown.csv",
        "paired-bootstrap.json",
        "pareto-frontier.json",
        "run-manifest.json",
        "blocked-and-unmapped.jsonl",
    }
    manifest = json.loads(
        (output / "run-manifest.json").read_text(encoding="utf-8")
    )
    validate_rq2_public_manifest(manifest)
    assert manifest["retrieval_scope"] == "paper-scoped"
    assert manifest["stage_anchors"]["pdf_chunker"] == "pdf-fixed-1200"
    assert any(
        row["status"] == "completed"
        and row["guardrails_passed"] is False
        for row in manifest["candidates"]
    )
    serialized = "\n".join(
        path.read_text(encoding="utf-8") for path in output.iterdir()
    )
    assert "F:\\private" not in serialized
    assert '"error"' not in serialized


@pytest.mark.parametrize(
    "corruption",
    (
        "completed-missing-execution-complete",
        "completed-execution-incomplete",
        "completed-missing-guardrail-finalized",
        "completed-guardrail-not-finalized",
        "completed-guardrails-passed-not-bool",
        "completed-mapping-passed-not-bool",
        "failed-missing-candidate",
        "failed-execution-complete",
        "failed-infrastructure-kind",
        "failed-missing-context",
        "failed-guardrail-finalized",
        "failed-empty-traceback",
    ),
)
def test_public_export_rejects_invalid_candidate_state_contract(
    tmp_path,
    corruption,
):
    run_root = _fixture_run(tmp_path)
    envelopes = [
        (path, json.loads(path.read_text(encoding="utf-8")))
        for path in (
            run_root / "sweep" / "candidates"
        ).rglob("*.json")
    ]
    target_status = (
        "completed" if corruption.startswith("completed-") else "failed"
    )
    path, envelope = next(
        item for item in envelopes if item[1]["status"] == target_status
    )
    payload = envelope["payload"]

    if corruption == "completed-missing-execution-complete":
        payload.pop("execution_complete")
    elif corruption == "completed-execution-incomplete":
        payload["execution_complete"] = False
    elif corruption == "completed-missing-guardrail-finalized":
        payload.pop("guardrail_finalized")
    elif corruption == "completed-guardrail-not-finalized":
        payload["guardrail_finalized"] = False
    elif corruption == "completed-guardrails-passed-not-bool":
        payload["guardrails_passed"] = "false"
    elif corruption == "completed-mapping-passed-not-bool":
        payload["mapping"]["coverage"]["passed"] = "false"
    elif corruption == "failed-missing-candidate":
        payload.pop("candidate")
    elif corruption == "failed-execution-complete":
        payload["execution_complete"] = True
    elif corruption == "failed-infrastructure-kind":
        payload["failure_kind"] = "infrastructure"
    elif corruption == "failed-missing-context":
        payload.pop("failure_context")
    elif corruption == "failed-guardrail-finalized":
        payload["guardrail_finalized"] = True
    else:
        payload["traceback"] = ""

    envelope["payload_sha256"] = fingerprint_payload(payload)
    _write_json(path, envelope)

    with pytest.raises(
        public_export.RQ2PublicExportError,
        match=r"candidate (completion|failure) gates failed",
    ):
        public_export._candidate_envelopes(run_root)
