from __future__ import annotations

import copy
import subprocess
import sys
from pathlib import Path

import yaml

from benchmarks.overnight import fingerprint_payload
from benchmarks.researchqa_strategy import StrategyCandidate
from benchmarks.researchqa_sweep import (
    SWEEP_ENGINE_REVISION,
    SWEEP_SCHEMA_VERSION,
)
from benchmarks.researchqa_validity_audit import (
    audit_candidate_envelope,
    expected_frozen_candidates,
)


ROOT = Path(__file__).resolve().parents[1]


def _candidate(*, rankable: bool = True) -> StrategyCandidate:
    return StrategyCandidate(
        stage_id="retriever",
        config_id="retriever-test",
        pdf_chunker="pdf-fixed-1200",
        note_chunker=None,
        retriever="dense",
        source_composition="pdf-only",
        reranker="rerank-off",
        reranker_depth=None,
        rankable=rankable,
    )


def _envelope(
    candidate: StrategyCandidate,
    *,
    status: str = "completed",
    guardrails_passed: bool = True,
    failure_kind: str = "strategy",
) -> dict[str, object]:
    if status == "completed":
        question_ids = [f"q-{index:03d}" for index in range(254)]
        payload: dict[str, object] = {
            "candidate": candidate.to_dict(),
            "completed_paper_ids": [
                f"W{index:010d}" for index in range(20)
            ],
            "completed_question_ids": question_ids,
            "execution_complete": True,
            "guardrail_finalized": True,
            "guardrails_passed": guardrails_passed,
            "metric_bundle_complete": True,
            "mapping": {
                "coverage": {
                    "passed": True,
                    "mapped_groups": 380,
                    "total_groups": 380,
                }
            },
            "primary_score": 0.8,
            "question_results": [
                {
                    "row_id": row_id,
                    "metrics": {
                        "coverage_ndcg_at_10": (
                            0.8 if index < 239 else None
                        )
                    },
                }
                for index, row_id in enumerate(question_ids)
            ],
            "retrieval_scope": "paper-scoped",
        }
    else:
        payload = {
            "candidate": candidate.to_dict(),
            "execution_complete": False,
            "failure_kind": failure_kind,
            "failure_context": {"phase": "chunking"},
            "error": "failed",
            "error_type": "StrategyContractError",
            "traceback": "traceback",
            "guardrail_finalized": False,
        }
    return {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "engine_revision": SWEEP_ENGINE_REVISION,
        "config_id": candidate.config_id,
        "stage_id": candidate.stage_id,
        "input_fingerprint": "a" * 64,
        "status": status,
        "payload_sha256": fingerprint_payload(payload),
        "payload": payload,
    }


def test_expected_frozen_candidates_are_exact_35() -> None:
    config = yaml.safe_load(
        (
            ROOT / "benchmarks" / "configs" / "rq2-overnight.yaml"
        ).read_text(encoding="utf-8")
    )
    candidates = expected_frozen_candidates(config)
    stage_counts: dict[str, int] = {}
    for candidate in candidates.values():
        stage_counts[candidate.stage_id] = (
            stage_counts.get(candidate.stage_id, 0) + 1
        )
    assert len(candidates) == 35
    assert stage_counts == {
        "pdf-chunker": 7,
        "note-chunker": 4,
        "retriever": 3,
        "source-composition": 5,
        "reranker": 4,
        "top2-confirmation": 12,
    }


def test_candidate_audit_separates_valid_poor_and_diagnostic() -> None:
    candidate = _candidate()
    passed = audit_candidate_envelope(
        _envelope(candidate),
        expected_candidate=candidate,
    )
    poor = audit_candidate_envelope(
        _envelope(candidate, guardrails_passed=False),
        expected_candidate=candidate,
    )
    diagnostic_candidate = _candidate(rankable=False)
    diagnostic = audit_candidate_envelope(
        _envelope(diagnostic_candidate),
        expected_candidate=diagnostic_candidate,
    )
    assert passed.validity_class == "valid-and-rankable"
    assert poor.validity_class == "valid-but-poor"
    assert diagnostic.validity_class == "diagnostic-only/ineligible"


def test_candidate_audit_separates_strategy_and_infrastructure_failures() -> None:
    candidate = _candidate()
    strategy = audit_candidate_envelope(
        _envelope(candidate, status="failed", failure_kind="strategy"),
        expected_candidate=candidate,
    )
    infrastructure = audit_candidate_envelope(
        _envelope(
            candidate,
            status="failed",
            failure_kind="infrastructure",
        ),
        expected_candidate=candidate,
    )
    assert strategy.validity_class == "deterministic-strategy-failure"
    assert infrastructure.validity_class == "infrastructure/unknown"


def test_candidate_audit_marks_truthy_or_tampered_completion_invalid() -> None:
    candidate = _candidate()
    envelope = _envelope(candidate)
    tampered = copy.deepcopy(envelope)
    tampered["payload"]["guardrails_passed"] = "false"
    tampered["payload_sha256"] = fingerprint_payload(tampered["payload"])
    row = audit_candidate_envelope(
        tampered,
        expected_candidate=candidate,
    )
    assert row.validity_class == "invalid-false-score"
    assert "guardrail-pass-not-boolean" in row.contract_errors

    broken_hash = copy.deepcopy(envelope)
    broken_hash["payload"]["primary_score"] = 0.9
    row = audit_candidate_envelope(
        broken_hash,
        expected_candidate=candidate,
    )
    assert row.validity_class == "invalid-false-score"
    assert "payload-sha-invalid" in row.contract_errors


def test_validity_audit_script_bootstraps_repository_imports(
    tmp_path: Path,
) -> None:
    completed = subprocess.run(
        [
            sys.executable,
            str(
                ROOT
                / "benchmarks"
                / "scripts"
                / "audit_rq2_strategy_results.py"
            ),
            "--help",
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "Audit all 35 persisted rq-2 strategy candidates" in completed.stdout
