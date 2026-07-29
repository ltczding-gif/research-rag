from __future__ import annotations

import pytest

from benchmarks.public_report import (
    PublicReportError,
    sanitize_hits,
    sanitize_rq2_blocked_rows,
    validate_rq2_public_manifest,
)


def test_sanitize_hits_uses_an_explicit_public_allowlist():
    internal_hit = {
        "paper_id": "paper-001",
        "file_id": "paper-001-main",
        "pdf_page_index": 7,
        "evidence_id": "evidence-009",
        "pdf_path": r"C:\Users\Private\Zotero\storage\ABCD1234\paper.pdf",
        "vault_path": r"C:\Users\Private\research-note\secret.md",
        "document": "unlicensed verbatim source text",
        "query": "private query",
        "api_key": "should-never-appear",
        "score": 0.93,
    }

    public_hits = sanitize_hits([internal_hit])

    assert public_hits == [
        {
            "paper_id": "paper-001",
            "file_id": "paper-001-main",
            "pdf_page_index": 7,
            "evidence_id": "evidence-009",
        }
    ]
    serialized = repr(public_hits)
    assert "Private" not in serialized
    assert "unlicensed" not in serialized
    assert "api_key" not in serialized


@pytest.mark.parametrize(
    "hit",
    [
        {"paper_id": r"c:\private\paper", "file_id": "file-1"},
        {"paper_id": "paper-1", "file_id": "../private"},
        {"paper_id": "paper-1", "file_id": "file-1", "evidence_id": "BAD ID"},
        {"paper_id": "paper-1", "file_id": "file-1", "pdf_page_index": -1},
    ],
)
def test_sanitizer_fails_closed_on_unsafe_public_fields(hit):
    with pytest.raises(PublicReportError):
        sanitize_hits([hit])


def test_sanitizer_requires_stable_public_identifiers():
    with pytest.raises(PublicReportError, match="paper_id"):
        sanitize_hits([{"file_id": "file-1"}])


def test_sanitizer_accepts_researchqa_openalex_and_source_ids():
    assert sanitize_hits(
        [
            {
                "paper_id": "W2792307011",
                "file_id": "Main",
                "pdf_page_index": 3,
                "evidence_id": "eg-555c873a816f3894ad51",
            },
            {
                "paper_id": "W2792307011",
                "file_id": "SI-01",
                "pdf_page_index": 1,
            },
        ]
    ) == [
        {
            "paper_id": "W2792307011",
            "file_id": "Main",
            "pdf_page_index": 3,
            "evidence_id": "eg-555c873a816f3894ad51",
        },
        {
            "paper_id": "W2792307011",
            "file_id": "SI-01",
            "pdf_page_index": 1,
            "evidence_id": None,
        },
    ]


def test_rq2_blocked_rows_use_kind_specific_allowlists():
    rows = sanitize_rq2_blocked_rows(
        [
            {
                "kind": "candidate",
                "stage_id": "pdf-chunker",
                "config_id": "pdf-chunker-deadbeef",
                "status": "failed",
                "error": r"F:\private\paper.pdf: parser failure",
                "rankable": True,
                "mapping_passed": False,
                "guardrails_passed": False,
                "api_key": "must-not-leak",
            },
            {
                "kind": "unmapped-evidence",
                "stage_id": "retriever",
                "config_id": "retriever-deadbeef",
                "row_id": "W2792307011_chunk0_lookup",
                "paper_id": "W2792307011",
                "group_id": "eg-555c873a816f3894ad51",
                "alternatives": ["unlicensed source passage"],
                "pdf_path": r"F:\private\paper.pdf",
            },
        ]
    )

    assert rows == [
        {
            "kind": "candidate",
            "stage_id": "pdf-chunker",
            "config_id": "pdf-chunker-deadbeef",
            "status": "failed",
            "rankable": True,
            "mapping_passed": False,
            "guardrails_passed": False,
        },
        {
            "kind": "unmapped-evidence",
            "stage_id": "retriever",
            "config_id": "retriever-deadbeef",
            "row_id": "W2792307011_chunk0_lookup",
            "paper_id": "W2792307011",
            "group_id": "eg-555c873a816f3894ad51",
        },
    ]
    serialized = repr(rows)
    assert "private" not in serialized.lower()
    assert "unlicensed" not in serialized
    assert "api_key" not in serialized


def test_rq2_blocked_rows_reject_unknown_kinds():
    with pytest.raises(PublicReportError, match="kind"):
        sanitize_rq2_blocked_rows([{"kind": "raw-result"}])


def _valid_rq2_public_manifest():
    stage_counts = {
        "pdf-chunker": 7,
        "note-chunker": 4,
        "retriever": 3,
        "source-composition": 5,
        "reranker": 4,
        "top2-confirmation": 12,
    }
    candidates = []
    for stage_id, count in stage_counts.items():
        for index in range(count):
            candidates.append(
                {
                    "config_id": f"{stage_id}-{index:02d}",
                    "stage_id": stage_id,
                    "status": (
                        "failed"
                        if stage_id == "pdf-chunker" and index == count - 1
                        else "completed"
                    ),
                    "rankable": not (
                        stage_id == "note-chunker" and index == 0
                    ),
                    "mapping_passed": not (
                        stage_id == "pdf-chunker" and index == count - 1
                    ),
                    "guardrails_passed": not (
                        stage_id == "pdf-chunker" and index == count - 1
                    ),
                }
            )
    winner = next(
        candidate["config_id"]
        for candidate in candidates
        if candidate["stage_id"] == "top2-confirmation"
    )
    baseline = next(
        candidate["config_id"]
        for candidate in candidates
        if candidate["stage_id"] == "pdf-chunker"
        and candidate["status"] == "completed"
    )
    extensions = [
        {
            "extension_id": extension_id,
            "config_id": f"repair-{extension_id.lower()}-fixture",
            "stage_id": "reranker",
            "status": "completed",
            "validity_class": (
                "valid-and-rankable" if passed else "valid-but-poor"
            ),
            "rankable": True,
            "mapping_passed": True,
            "guardrails_passed": passed,
            "baseline_config_id": baseline,
            "primary_score": 0.85 if passed else 0.75,
            "primary_delta": 0.01 if passed else -0.09,
            "p95_latency_ms": 100.0,
            "input_fingerprint": "1" * 64,
            "payload_sha256": "2" * 64,
            "progress_payload_sha256": "3" * 64,
            "code_fingerprint": "4" * 64,
        }
        for extension_id, passed in (
            ("F2", False),
            ("RR1", True),
            ("R1", True),
            ("S1", False),
        )
    ]
    return {
        "schema_version": 1,
        "run_id": "rq2-public",
        "status": "completed",
        "created_at": "2026-07-28T00:00:00Z",
        "updated_at": "2026-07-28T12:00:00Z",
        "budget_seconds": 36_000,
        "elapsed_seconds": 40_000,
        "fingerprints": {
            "code": "code-sha256",
            "config": "config-sha256",
            "embedding-model": "embedding-sha256",
            "reranker-model": "reranker-sha256",
            "data": "data-sha256",
        },
        "hardware_fingerprints": {
            "platform": "platform-sha256",
            "cpu": "cpu-sha256",
            "gpu": "gpu-sha256",
        },
        "task_counts": {
            "pending": 0,
            "running": 0,
            "completed": 2,
            "failed": 0,
            "blocked": 0,
        },
        "retrieval_scope": "paper-scoped",
        "stage_anchors": {
            "pdf_chunker": "pdf-fixed-1200",
            "note_chunker": "note-reviewer-concern",
            "retriever": "hybrid-rrf",
            "source_composition": "pdf-only",
        },
        "mapping_coverage": {
            "passed": True,
            "overall": 1.0,
            "mapped_groups": 380,
            "total_groups": 380,
            "per_paper": {
                f"W{index:010d}": 1.0
                for index in range(20)
            },
        },
        "candidates": candidates,
        "approved_extensions": extensions,
        "reconciliation": {
            "schema_version": 1,
            "revision": "rq2-superseding-reconciliation-v1",
            "status": "completed",
            "payload_sha256": "5" * 64,
            "source_run_state_sha256": "6" * 64,
            "source_run_status": "partial",
            "superseded_failure_count": 1,
            "baseline_candidate_count": 35,
            "baseline_classification_counts": {
                "valid-and-rankable": 6,
                "valid-but-poor": 26,
                "diagnostic-only/ineligible": 2,
                "deterministic-strategy-failure": 1,
                "infrastructure/unknown": 0,
                "invalid-false-score": 0,
            },
            "approved_extension_config_ids": [
                row["config_id"] for row in extensions
            ],
            "note_prequality": {
                "status": "passed",
                "paper_count": 20,
                "eligible_paper_count": 20,
                "fallback_paper_count": 0,
                "artifact_sha256": "9" * 64,
            },
            "effective_task_counts": {
                "pending": 0,
                "running": 0,
                "completed": 2,
                "failed": 0,
                "blocked": 0,
            },
            "provisional_winner": winner,
            "stop_after_report": True,
            "rq5_started": False,
            "code_fingerprint": "7" * 64,
            "data_inputs_fingerprint": "8" * 64,
        },
        "confirmation_plan": {
            "cartesian_rows": 16,
            "unique_candidates": 12,
            "deduplicated_aliases": 4,
            "compatibility_rule": (
                "hierarchical-pdf-requires-pdf-parent-child"
            ),
            "pdf_chunkers": ["pdf-fixed-800", "pdf-fixed-1200"],
            "retrievers": ["dense", "hybrid-rrf"],
            "source_compositions": ["pdf-only", "hierarchical-pdf"],
            "reranker_modes": ["rerank-off", "rerank-50-to-10"],
        },
        "bootstrap": {
            "samples": 10_000,
            "confidence": 0.95,
            "candidate_config_id": winner,
            "baseline_config_id": baseline,
            "observed_delta": 0.01,
            "lower": -0.01,
            "upper": 0.03,
        },
        "pareto_frontier": [{"config_id": winner}],
        "provisional_winner": winner,
        "artifacts": [
            {
                "name": name,
                "bytes": 100 + index,
                "sha256": f"{index + 1:064x}",
            }
            for index, name in enumerate(
                (
                    "morning-report.md",
                    "detailed-strategy-analysis.html",
                    "leaderboard.csv",
                    "paper-domain-breakdown.csv",
                    "paired-bootstrap.json",
                    "pareto-frontier.json",
                    "blocked-and-unmapped.jsonl",
                    "reconciliation.json",
                )
            )
        ],
        "completed": ["sources-task", "runtime-task"],
        "partial": [],
        "blocked": [],
        "failed": [],
    }


def test_rq2_public_manifest_requires_every_final_completion_gate():
    validate_rq2_public_manifest(_valid_rq2_public_manifest())


def test_rq2_public_manifest_accepts_audited_extension_winner():
    payload = _valid_rq2_public_manifest()
    winner = next(
        row["config_id"]
        for row in payload["approved_extensions"]
        if row["extension_id"] == "RR1"
    )
    payload["provisional_winner"] = winner
    payload["bootstrap"]["candidate_config_id"] = winner
    payload["pareto_frontier"] = [{"config_id": winner}]
    payload["reconciliation"]["provisional_winner"] = winner

    validate_rq2_public_manifest(payload)


def test_rq2_public_manifest_requires_extensions_and_reconciliation():
    missing_extension = _valid_rq2_public_manifest()
    missing_extension["approved_extensions"].pop()
    with pytest.raises(PublicReportError, match="approved_extensions"):
        validate_rq2_public_manifest(missing_extension)

    task_mismatch = _valid_rq2_public_manifest()
    task_mismatch["reconciliation"]["effective_task_counts"]["completed"] = 1
    with pytest.raises(PublicReportError, match="task counts"):
        validate_rq2_public_manifest(task_mismatch)

    winner_mismatch = _valid_rq2_public_manifest()
    winner_mismatch["reconciliation"]["provisional_winner"] = (
        winner_mismatch["approved_extensions"][0]["config_id"]
    )
    with pytest.raises(PublicReportError, match="reconciliation winner"):
        validate_rq2_public_manifest(winner_mismatch)


def test_rq2_public_manifest_requires_paper_scoped_retrieval():
    payload = _valid_rq2_public_manifest()
    payload["retrieval_scope"] = "global-corpus"

    with pytest.raises(PublicReportError, match="retrieval scope"):
        validate_rq2_public_manifest(payload)


def test_rq2_public_manifest_rejects_guardrail_failed_winner():
    payload = _valid_rq2_public_manifest()
    winner = payload["provisional_winner"]
    next(
        candidate
        for candidate in payload["candidates"]
        if candidate["config_id"] == winner
    )["guardrails_passed"] = False

    with pytest.raises(PublicReportError, match="eligible Pareto"):
        validate_rq2_public_manifest(payload)


def test_rq2_public_manifest_rejects_other_completion_gaps():
    """Non-winner failures remain reportable; completion gaps do not."""

    invalid_cases = {
        "status": ("status", "running"),
        "hardware": ("hardware_fingerprints", {}),
        "candidates": ("candidates", _valid_rq2_public_manifest()["candidates"][:-1]),
        "confirmation": ("confirmation_plan", None),
        "mapping": ("mapping_coverage", None),
        "bootstrap": ("bootstrap", {"samples": 9_999, "confidence": 0.95}),
        "pareto": ("pareto_frontier", []),
        "winner": ("provisional_winner", None),
        "partial": ("partial", ["unfinished-task"]),
    }
    for expected_error, (field, value) in invalid_cases.items():
        payload = _valid_rq2_public_manifest()
        payload[field] = value
        with pytest.raises(PublicReportError, match=expected_error):
            validate_rq2_public_manifest(payload)

    low_mapping = _valid_rq2_public_manifest()
    low_mapping["mapping_coverage"]["per_paper"]["W0000000000"] = 0.89
    with pytest.raises(PublicReportError, match="mapping"):
        validate_rq2_public_manifest(low_mapping)

    duplicate_candidate = _valid_rq2_public_manifest()
    duplicate_candidate["candidates"][-1] = duplicate_candidate["candidates"][0]
    with pytest.raises(PublicReportError, match="candidates"):
        validate_rq2_public_manifest(duplicate_candidate)


def test_rq2_public_manifest_rejects_outer_task_or_fingerprint_gaps():
    task_failure = _valid_rq2_public_manifest()
    task_failure["task_counts"]["failed"] = 1
    with pytest.raises(PublicReportError, match="task"):
        validate_rq2_public_manifest(task_failure)

    missing_data = _valid_rq2_public_manifest()
    del missing_data["fingerprints"]["data"]
    with pytest.raises(PublicReportError, match="fingerprint"):
        validate_rq2_public_manifest(missing_data)

    unsafe_hardware = _valid_rq2_public_manifest()
    unsafe_hardware["hardware_fingerprints"]["gpu"] = "../private"
    with pytest.raises(PublicReportError, match="hardware"):
        validate_rq2_public_manifest(unsafe_hardware)


def test_rq2_public_manifest_rejects_mapping_contract_gaps():
    too_few_papers = _valid_rq2_public_manifest()
    too_few_papers["mapping_coverage"]["per_paper"].pop("W0000000019")
    with pytest.raises(PublicReportError, match="mapping"):
        validate_rq2_public_manifest(too_few_papers)

    not_passed = _valid_rq2_public_manifest()
    not_passed["mapping_coverage"]["passed"] = False
    with pytest.raises(PublicReportError, match="mapping"):
        validate_rq2_public_manifest(not_passed)

    inconsistent_groups = _valid_rq2_public_manifest()
    inconsistent_groups["mapping_coverage"]["mapped_groups"] = 381
    with pytest.raises(PublicReportError, match="mapping"):
        validate_rq2_public_manifest(inconsistent_groups)


def test_rq2_public_manifest_rejects_candidate_stage_or_terminal_gaps():
    wrong_stage_count = _valid_rq2_public_manifest()
    wrong_stage_count["candidates"][0]["stage_id"] = "retriever"
    with pytest.raises(PublicReportError, match="candidates"):
        validate_rq2_public_manifest(wrong_stage_count)

    incomplete = _valid_rq2_public_manifest()
    incomplete["candidates"][0]["status"] = "incomplete"
    with pytest.raises(PublicReportError, match="candidates"):
        validate_rq2_public_manifest(incomplete)

    wrong_confirmation_count = _valid_rq2_public_manifest()
    wrong_confirmation_count["confirmation_plan"]["unique_candidates"] = 16
    with pytest.raises(PublicReportError, match="confirmation"):
        validate_rq2_public_manifest(wrong_confirmation_count)


def test_rq2_public_manifest_rejects_decision_inconsistency():
    unknown_bootstrap_winner = _valid_rq2_public_manifest()
    unknown_bootstrap_winner["bootstrap"]["candidate_config_id"] = "unknown"
    with pytest.raises(PublicReportError, match="bootstrap"):
        validate_rq2_public_manifest(unknown_bootstrap_winner)

    invalid_interval = _valid_rq2_public_manifest()
    invalid_interval["bootstrap"]["lower"] = 0.02
    with pytest.raises(PublicReportError, match="bootstrap"):
        validate_rq2_public_manifest(invalid_interval)

    unknown_pareto = _valid_rq2_public_manifest()
    unknown_pareto["pareto_frontier"] = [{"config_id": "unknown"}]
    with pytest.raises(PublicReportError, match="pareto"):
        validate_rq2_public_manifest(unknown_pareto)

    winner_outside_pareto = _valid_rq2_public_manifest()
    alternate = next(
        candidate["config_id"]
        for candidate in winner_outside_pareto["candidates"]
        if candidate["stage_id"] == "top2-confirmation"
        and candidate["config_id"]
        != winner_outside_pareto["provisional_winner"]
    )
    winner_outside_pareto["provisional_winner"] = alternate
    with pytest.raises(PublicReportError, match="winner"):
        validate_rq2_public_manifest(winner_outside_pareto)


def test_rq2_public_manifest_rejects_unsafe_or_incomplete_artifacts():
    unsafe_name = _valid_rq2_public_manifest()
    unsafe_name["artifacts"][0]["name"] = "../morning-report.md"
    with pytest.raises(PublicReportError, match="artifact"):
        validate_rq2_public_manifest(unsafe_name)

    invalid_hash = _valid_rq2_public_manifest()
    invalid_hash["artifacts"][0]["sha256"] = "not-a-sha256"
    with pytest.raises(PublicReportError, match="artifact"):
        validate_rq2_public_manifest(invalid_hash)

    missing_artifact = _valid_rq2_public_manifest()
    missing_artifact["artifacts"].pop()
    with pytest.raises(PublicReportError, match="artifact"):
        validate_rq2_public_manifest(missing_artifact)
