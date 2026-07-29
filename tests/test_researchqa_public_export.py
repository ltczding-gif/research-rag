from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest
import yaml

from benchmarks.overnight import fingerprint_payload, sha256_path
from benchmarks.researchqa_reconciliation_contract import (
    EXPECTED_BASELINE_CLASS_COUNTS,
    EXPECTED_EFFECTIVE_TASK_IDS,
    RECONCILIATION_ARTIFACT_PATHS,
    RECONCILIATION_REVISION,
    reconciliation_code_fingerprint,
)
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

    winner = "repair-rr1-fixture"
    baseline = "pdf-chunker-00"
    _write_json(
        run_root / "sweep" / "final" / "superseding-decision-summary.json",
        {
            "provisional_winner": winner,
            "baseline_config_id": baseline,
            "primary_metric": "coverage_ndcg_at_10",
            "primary": 0.85,
            "primary_delta": 0.01,
            "guardrails_passed": True,
            "stop_after_report": True,
            "rq5_started": False,
        },
    )
    _write_json(
        run_root
        / "sweep"
        / "final"
        / "superseding-pareto-frontier.json",
        {
            "schema_version": 1,
            "rows": [
                {
                    "rank": 1,
                    "config_id": winner,
                    "stage_id": "reranker",
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
        run_root / "report" / "superseding-paired-bootstrap.json",
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
        run_root / "report" / "superseding-leaderboard.csv",
        public_export.LEADERBOARD_FIELDS,
        leaderboard_rows,
    )
    breakdown_fields = public_export.BREAKDOWN_FIELDS
    _write_csv(
        run_root
        / "report"
        / "superseding-paper-domain-breakdown.csv",
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
    (
        run_root
        / "report"
        / "superseding-blocked-and-unmapped.jsonl"
    ).write_text(
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
            "status": "partial",
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
                "sources-fixture": {
                    "status": "completed",
                },
                "runtime-fixture": {
                    "status": "failed",
                }
            },
        },
    )
    _write_json(
        run_root
        / "sweep"
        / "final"
        / "superseding-leaderboard.json",
        {"schema_version": 1, "rows": leaderboard_rows},
    )
    extension_rows = []
    for extension_id, passed in (
        ("F2", False),
        ("RR1", True),
        ("R1", True),
        ("S1", False),
    ):
        config_id = f"repair-{extension_id.lower()}-fixture"
        input_fingerprint = "1" * 64
        code_fingerprint = "4" * 64
        candidate_payload = {"fixture": f"{extension_id}-candidate"}
        candidate_envelope = {
            "config_id": config_id,
            "stage_id": "reranker",
            "input_fingerprint": input_fingerprint,
            "status": "completed",
            "payload_sha256": fingerprint_payload(candidate_payload),
            "payload": candidate_payload,
        }
        candidate_path = (
            run_root
            / "sweep"
            / "extensions"
            / extension_id
            / "candidates"
            / "reranker"
            / f"{config_id}.json"
        )
        _write_json(candidate_path, candidate_envelope)
        progress_payload = {
            "code_fingerprint": code_fingerprint,
            "fixture": f"{extension_id}-progress",
        }
        progress_envelope = {
            "config_id": config_id,
            "stage_id": "reranker",
            "input_fingerprint": input_fingerprint,
            "payload_sha256": fingerprint_payload(progress_payload),
            "payload": progress_payload,
        }
        progress_path = (
            run_root
            / "sweep"
            / "progress"
            / "reranker"
            / config_id
            / f"{input_fingerprint}.json"
        )
        _write_json(progress_path, progress_envelope)
        runtime_root = (
            run_root / "sweep" / "extensions" / extension_id / "runtime"
        )
        runtime_path = runtime_root / "runtime-summary.json"
        prequality_path = runtime_root / "prequality.json"
        preflight_path = runtime_root / "model-preflight.json"
        _write_json(runtime_path, {"fixture": f"{extension_id}-runtime"})
        _write_json(prequality_path, {"fixture": f"{extension_id}-prequality"})
        _write_json(preflight_path, {"fixture": f"{extension_id}-preflight"})
        extension_rows.append(
            {
                "extension_id": extension_id,
                "config_id": config_id,
                "stage_id": "reranker",
                "status": "completed",
                "validity_class": (
                    "valid-and-rankable"
                    if passed
                    else "valid-but-poor"
                ),
                "rankable": True,
                "mapping_passed": True,
                "guardrails_passed": passed,
                "contract_errors": [],
                "baseline_config_id": baseline,
                "primary_metric": "coverage_ndcg_at_10",
                "primary_score": 0.85 if passed else 0.75,
                "baseline_primary_score": 0.84,
                "primary_delta": 0.01 if passed else -0.09,
                "new_hard_failure_count": 0 if passed else 1,
                "p95_latency_ms": 100.0,
                "latency_validity": "observed-only",
                "index_bytes": 1000,
                "chunk_count": 100,
                "input_fingerprint": input_fingerprint,
                "payload_sha256": candidate_envelope["payload_sha256"],
                "progress_payload_sha256": progress_envelope["payload_sha256"],
                "code_fingerprint": code_fingerprint,
                "candidate_file_sha256": sha256_path(candidate_path)[1],
                "progress_file_sha256": sha256_path(progress_path)[1],
                "runtime_summary_sha256": sha256_path(runtime_path)[1],
                "prequality_sha256": sha256_path(prequality_path)[1],
                "model_preflight_sha256": sha256_path(preflight_path)[1],
                "candidate": {},
            }
        )
    effective_tasks = [
        {"task_id": task_id, "status": "completed"}
        for task_id in sorted(EXPECTED_EFFECTIVE_TASK_IDS)
    ]
    artifact_rows = []
    for relative in sorted(RECONCILIATION_ARTIFACT_PATHS):
        size, digest = sha256_path(run_root / relative)
        artifact_rows.append(
            {"path": relative, "bytes": size, "sha256": digest}
        )
    _size, state_sha = sha256_path(run_root / "run-state.json")
    n0_prequality_path = (
        run_root
        / "sweep"
        / "extensions"
        / "N0-N3"
        / "runtime"
        / "prequality.json"
    )
    _write_json(n0_prequality_path, {"fixture": "N0-N3-prequality"})
    reconciliation_payload = {
        "status": "completed",
        "public_export_ready": True,
        "run_id": "rq2-fixture",
        "revision": RECONCILIATION_REVISION,
        "fingerprints": {
            "code": reconciliation_code_fingerprint(),
            "config": fingerprint_payload(config),
            "embedding-model": "b" * 64,
            "reranker-model": "c" * 64,
            "data-inputs": public_export._data_fingerprint(run_root),
        },
        "source_run_state": {
            "status": "partial",
            "sha256": state_sha,
            "completed_task_ids": ["sources-fixture"],
            "superseded_failed_task_ids": ["runtime-fixture"],
            "superseded_failure_count": 1,
        },
        "baseline_audit": {
            "candidate_count": 35,
            "validity_gate_closed": True,
            "classification_counts": EXPECTED_BASELINE_CLASS_COUNTS,
            "stage_counts": public_export.EXPECTED_STAGE_COUNTS,
            "rows_fingerprint": "d" * 64,
            "candidate_artifacts": [
                {
                    "stage_id": stage_id,
                    "config_id": config_id,
                    "file_sha256": sha256_path(
                        (
                            run_root
                            / "sweep"
                            / "candidates"
                            / stage_id
                            / f"{config_id}.json"
                        )
                    )[1],
                }
                for stage_id, config_id, _failed in candidates
            ],
        },
        "note_prequality": {
            "status": "passed",
            "paper_count": 20,
            "eligible_paper_count": 20,
            "fallback_paper_count": 0,
            "base_chunk_count": 20,
            "backlinkable_base_chunk_count": 20,
            "reviewer_chunk_count": 2,
            "backlinkable_reviewer_chunk_count": 2,
            "diagnostic_fingerprint": "e" * 64,
            "artifact_sha256": sha256_path(n0_prequality_path)[1],
        },
        "approved_extensions": extension_rows,
        "effective_tasks": effective_tasks,
        "effective_task_counts": {
            "pending": 0,
            "running": 0,
            "completed": len(effective_tasks),
            "failed": 0,
            "blocked": 0,
        },
        "decision": {
            "provisional_winner": winner,
            "baseline_config_id": baseline,
            "primary_metric": "coverage_ndcg_at_10",
            "primary_score": 0.85,
            "primary_delta": 0.01,
            "guardrails_passed": True,
            "pareto_config_ids": [winner],
            "stop_after_report": True,
            "rq5_started": False,
        },
        "artifacts": artifact_rows,
    }
    _write_json(
        run_root
        / "sweep"
        / "final"
        / "superseding-reconciliation.json",
        {
            "schema_version": 1,
            "revision": RECONCILIATION_REVISION,
            "run_id": "rq2-fixture",
            "payload_sha256": fingerprint_payload(reconciliation_payload),
            "payload": reconciliation_payload,
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
        "reconciliation.json",
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


def test_public_export_rejects_tampered_reconciliation_artifact(tmp_path):
    run_root = _fixture_run(tmp_path)
    leaderboard = (
        run_root / "report" / "superseding-leaderboard.csv"
    )
    leaderboard.write_text(
        leaderboard.read_text(encoding="utf-8") + "tampered\n",
        encoding="utf-8",
    )

    with pytest.raises(
        public_export.RQ2PublicExportError,
        match="artifact hash mismatch",
    ):
        public_export.export_rq2_public_report(
            run_root,
            output_root=tmp_path / "public" / "researchqa-rq2",
        )


def test_public_export_rejects_tampered_extension_evidence(tmp_path):
    run_root = _fixture_run(tmp_path)
    candidate = next(
        (
            run_root
            / "sweep"
            / "extensions"
            / "RR1"
            / "candidates"
        ).rglob("*.json")
    )
    candidate.write_text(
        candidate.read_text(encoding="utf-8") + " ",
        encoding="utf-8",
    )

    with pytest.raises(
        public_export.RQ2PublicExportError,
        match="RR1 candidate hash mismatch",
    ):
        public_export.export_rq2_public_report(
            run_root,
            output_root=tmp_path / "public" / "researchqa-rq2",
        )


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
