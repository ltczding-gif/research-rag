"""Fail-closed validity audit for the frozen rq-2 strategy matrix.

The sweep runner decides whether a candidate is complete and eligible while it
is running.  This module provides a separate, read-only audit of the persisted
candidate envelopes so stale outer-run state and valid-but-poor strategy
results cannot be confused with invalid scores.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from benchmarks.overnight import fingerprint_payload
from benchmarks.researchqa_strategy import (
    ConfirmationSelection,
    StrategyCandidate,
    generate_orthogonal_candidates,
)
from benchmarks.researchqa_reconciliation_contract import (
    ReconciliationContractError,
    load_rq2_reconciliation,
)
from benchmarks.researchqa_sweep import (
    SWEEP_ENGINE_REVISION,
    SWEEP_SCHEMA_VERSION,
)


EXPECTED_STAGE_COUNTS = {
    "pdf-chunker": 7,
    "note-chunker": 4,
    "retriever": 3,
    "source-composition": 5,
    "reranker": 4,
    "top2-confirmation": 12,
}
VALIDITY_CLASSES = (
    "valid-and-rankable",
    "valid-but-poor",
    "diagnostic-only/ineligible",
    "deterministic-strategy-failure",
    "infrastructure/unknown",
    "invalid-false-score",
)


class ValidityAuditError(ValueError):
    """Raised when the frozen audit plan itself is inconsistent."""


@dataclass(frozen=True)
class CandidateValidityRow:
    stage_id: str
    config_id: str
    pdf_chunker: str
    note_chunker: str
    retriever: str
    source_composition: str
    reranker: str
    rankable: bool
    status: str
    validity_class: str
    primary_score: float | None
    guardrails_passed: bool
    guardrail_failures: str
    new_hard_failure_count: int
    operational_review_required: bool
    latency_validity: str
    input_fingerprint: str
    payload_sha256: str
    contract_errors: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class StrategyValidityAudit:
    run_id: str
    rows: tuple[CandidateValidityRow, ...]
    classification_counts: Mapping[str, int]
    stage_counts: Mapping[str, int]
    outer_task_counts: Mapping[str, int]
    historical_outer_task_counts: Mapping[str, int]
    reconciliation_status: str
    baseline_validity_gate_closed: bool
    public_export_ready: bool
    public_export_blockers: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "run_id": self.run_id,
            "candidate_count": len(self.rows),
            "classification_counts": dict(self.classification_counts),
            "stage_counts": dict(self.stage_counts),
            "outer_task_counts": dict(self.outer_task_counts),
            "historical_outer_task_counts": dict(
                self.historical_outer_task_counts
            ),
            "reconciliation_status": self.reconciliation_status,
            "baseline_validity_gate_closed": self.baseline_validity_gate_closed,
            "public_export_ready": self.public_export_ready,
            "public_export_blockers": list(self.public_export_blockers),
            "rows": [row.to_dict() for row in self.rows],
        }


def _required_mapping(
    value: object,
    label: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidityAuditError(f"{label} must be a mapping")
    return value


def _required_pair(
    value: object,
    label: str,
) -> tuple[str, str]:
    if (
        not isinstance(value, list)
        or len(value) != 2
        or len(set(value)) != 2
        or any(not isinstance(item, str) or not item for item in value)
    ):
        raise ValidityAuditError(f"{label} must contain two unique IDs")
    return str(value[0]), str(value[1])


def expected_frozen_candidates(
    config: Mapping[str, Any],
) -> Mapping[str, StrategyCandidate]:
    """Regenerate the exact frozen 7/4/3/5/4/12 candidate plan."""

    stages = _required_mapping(config.get("stages"), "config.stages")
    anchors = _required_mapping(
        stages.get("stage_anchors"),
        "config.stages.stage_anchors",
    )
    required_anchors = {
        "pdf_chunker",
        "note_chunker",
        "retriever",
        "source_composition",
    }
    if set(anchors) != required_anchors or any(
        not isinstance(anchors.get(key), str) or not anchors[key]
        for key in required_anchors
    ):
        raise ValidityAuditError("frozen stage anchors are incomplete")
    confirmation_config = _required_mapping(
        stages.get("top2_confirmation"),
        "config.stages.top2_confirmation",
    )
    confirmation = ConfirmationSelection(
        pdf_chunkers=_required_pair(
            confirmation_config.get("selected_pdf_chunkers"),
            "selected_pdf_chunkers",
        ),
        retrievers=_required_pair(
            confirmation_config.get("selected_retrievers"),
            "selected_retrievers",
        ),
        source_compositions=_required_pair(
            confirmation_config.get("selected_source_compositions"),
            "selected_source_compositions",
        ),
        reranker_modes=_required_pair(
            confirmation_config.get("selected_reranker_modes"),
            "selected_reranker_modes",
        ),
    )
    if confirmation.reranker_modes[0] != "rerank-off":
        raise ValidityAuditError(
            "frozen confirmation must put rerank-off first"
        )

    pdf_plan = generate_orthogonal_candidates(config)
    note_plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker=str(anchors["pdf_chunker"]),
    )
    retrieval_plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker=str(anchors["pdf_chunker"]),
        anchor_note_chunker=str(anchors["note_chunker"]),
    )
    composition_plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker=str(anchors["pdf_chunker"]),
        anchor_note_chunker=str(anchors["note_chunker"]),
        anchor_retriever=str(anchors["retriever"]),
    )
    reranker_plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker=str(anchors["pdf_chunker"]),
        anchor_note_chunker=str(anchors["note_chunker"]),
        anchor_retriever=str(anchors["retriever"]),
        anchor_source_composition=str(anchors["source_composition"]),
    )
    confirmation_plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker=str(anchors["pdf_chunker"]),
        anchor_note_chunker=str(anchors["note_chunker"]),
        anchor_retriever=str(anchors["retriever"]),
        anchor_source_composition=str(anchors["source_composition"]),
        best_reranker=confirmation.reranker_modes[1],
        confirmation=confirmation,
    )
    plans = (
        pdf_plan.stages["pdf-chunker"],
        note_plan.stages["note-chunker"],
        retrieval_plan.stages["retriever"],
        composition_plan.stages["source-composition"],
        reranker_plan.stages["reranker"],
        confirmation_plan.stages["top2-confirmation"],
    )
    candidates = {
        candidate.config_id: candidate
        for plan in plans
        for candidate in plan
    }
    stage_counts = Counter(
        candidate.stage_id for candidate in candidates.values()
    )
    if (
        len(candidates) != sum(EXPECTED_STAGE_COUNTS.values())
        or dict(stage_counts) != EXPECTED_STAGE_COUNTS
    ):
        raise ValidityAuditError(
            "frozen candidate plan is not the expected 7/4/3/5/4/12 matrix"
        )
    return candidates


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _completed_contract_errors(
    payload: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    mapping = payload.get("mapping")
    coverage = (
        mapping.get("coverage") if isinstance(mapping, Mapping) else None
    )
    papers = payload.get("completed_paper_ids")
    questions = payload.get("completed_question_ids")
    question_results = payload.get("question_results")
    if payload.get("execution_complete") is not True:
        errors.append("execution-not-complete")
    if payload.get("guardrail_finalized") is not True:
        errors.append("guardrail-not-finalized")
    if not isinstance(payload.get("guardrails_passed"), bool):
        errors.append("guardrail-pass-not-boolean")
    if payload.get("retrieval_scope") != "paper-scoped":
        errors.append("retrieval-scope-not-paper-scoped")
    if (
        not isinstance(coverage, Mapping)
        or coverage.get("passed") is not True
        or coverage.get("mapped_groups") != 380
        or coverage.get("total_groups") != 380
    ):
        errors.append("mapping-coverage-invalid")
    if (
        not isinstance(papers, list)
        or len(papers) != 20
        or len(set(map(str, papers))) != 20
    ):
        errors.append("paper-set-invalid")
    if (
        not isinstance(questions, list)
        or len(questions) != 254
        or len(set(map(str, questions))) != 254
    ):
        errors.append("question-set-invalid")
    if not isinstance(question_results, list) or len(question_results) != 254:
        errors.append("question-results-invalid")
    else:
        row_ids = [str(row.get("row_id")) for row in question_results]
        if len(set(row_ids)) != 254 or (
            isinstance(questions, list)
            and set(row_ids) != set(map(str, questions))
        ):
            errors.append("question-result-row-set-invalid")
        evaluable = sum(
            isinstance(row.get("metrics"), Mapping)
            and row["metrics"].get("coverage_ndcg_at_10") is not None
            for row in question_results
            if isinstance(row, Mapping)
        )
        if evaluable != 239:
            errors.append("evaluable-question-count-invalid")
    if payload.get("metric_bundle_complete") is not True:
        errors.append("metric-bundle-incomplete")
    return errors


def _failed_contract_errors(
    payload: Mapping[str, Any],
) -> list[str]:
    errors: list[str] = []
    if payload.get("execution_complete") is not False:
        errors.append("failed-execution-state-invalid")
    if payload.get("guardrail_finalized") is not False:
        errors.append("failed-guardrail-state-invalid")
    if payload.get("failure_kind") not in {
        "strategy",
        "infrastructure",
        "unknown",
    }:
        errors.append("failure-kind-invalid")
    if not isinstance(payload.get("failure_context"), Mapping):
        errors.append("failure-context-invalid")
    for key in ("error", "error_type", "traceback"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            errors.append(f"{key}-invalid")
    return errors


def audit_candidate_envelope(
    envelope: object,
    *,
    expected_candidate: StrategyCandidate,
) -> CandidateValidityRow:
    """Classify one candidate without trusting its status or pass booleans."""

    errors: list[str] = []
    outer = envelope if isinstance(envelope, Mapping) else {}
    payload = outer.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    candidate = payload.get("candidate")
    candidate = candidate if isinstance(candidate, Mapping) else {}
    status = str(outer.get("status") or "missing")
    input_fingerprint = str(outer.get("input_fingerprint") or "")
    payload_sha256 = str(outer.get("payload_sha256") or "")

    if outer.get("schema_version") != SWEEP_SCHEMA_VERSION:
        errors.append("schema-version-invalid")
    if outer.get("engine_revision") != SWEEP_ENGINE_REVISION:
        errors.append("engine-revision-invalid")
    if outer.get("config_id") != expected_candidate.config_id:
        errors.append("config-id-invalid")
    if outer.get("stage_id") != expected_candidate.stage_id:
        errors.append("stage-id-invalid")
    if not _is_sha256(input_fingerprint):
        errors.append("input-fingerprint-invalid")
    if (
        not _is_sha256(payload_sha256)
        or payload_sha256 != fingerprint_payload(payload)
    ):
        errors.append("payload-sha-invalid")
    if candidate != expected_candidate.to_dict():
        errors.append("candidate-identity-invalid")
    if status == "completed":
        errors.extend(_completed_contract_errors(payload))
    elif status == "failed":
        errors.extend(_failed_contract_errors(payload))
    else:
        errors.append("candidate-status-invalid")

    rankable = candidate.get("rankable") is True
    if errors:
        validity_class = "invalid-false-score"
    elif status == "failed":
        validity_class = (
            "deterministic-strategy-failure"
            if payload.get("failure_kind") == "strategy"
            else "infrastructure/unknown"
        )
    elif not rankable:
        validity_class = "diagnostic-only/ineligible"
    elif payload.get("guardrails_passed") is True:
        validity_class = "valid-and-rankable"
    else:
        validity_class = "valid-but-poor"

    diagnostics = payload.get("guardrail_diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
    failures = diagnostics.get("failures")
    failures = (
        [str(item) for item in failures]
        if isinstance(failures, list)
        else []
    )
    hard_failures = diagnostics.get("new_recall_at_10_hard_failure_ids")
    hard_failures = hard_failures if isinstance(hard_failures, list) else []
    latency = payload.get("latency_metrics")
    latency = latency if isinstance(latency, Mapping) else {}
    latency_validity = str(latency.get("validity") or "observed-only")
    if latency_validity not in {"decisive", "observed-only"}:
        errors.append("latency-validity-invalid")
        validity_class = "invalid-false-score"

    primary = payload.get("primary_score")
    return CandidateValidityRow(
        stage_id=expected_candidate.stage_id,
        config_id=expected_candidate.config_id,
        pdf_chunker=expected_candidate.pdf_chunker,
        note_chunker=expected_candidate.note_chunker or "",
        retriever=expected_candidate.retriever,
        source_composition=expected_candidate.source_composition,
        reranker=expected_candidate.reranker,
        rankable=expected_candidate.rankable,
        status=status,
        validity_class=validity_class,
        primary_score=(
            float(primary) if isinstance(primary, (int, float)) else None
        ),
        guardrails_passed=payload.get("guardrails_passed") is True,
        guardrail_failures="|".join(failures),
        new_hard_failure_count=len(hard_failures),
        operational_review_required=(
            diagnostics.get("operational_review_required") is True
        ),
        latency_validity=latency_validity,
        input_fingerprint=input_fingerprint,
        payload_sha256=payload_sha256,
        contract_errors="|".join(errors),
    )


def _outer_task_counts(state: object) -> Mapping[str, int]:
    statuses = ("pending", "running", "completed", "failed", "blocked")
    tasks = state.get("tasks") if isinstance(state, Mapping) else None
    if not isinstance(tasks, Mapping):
        return {status: 0 for status in statuses} | {"unknown": 1}
    counts = Counter(
        str(task.get("status") or "unknown")
        for task in tasks.values()
        if isinstance(task, Mapping)
    )
    return {status: counts[status] for status in statuses} | {
        "unknown": sum(
            count for status, count in counts.items() if status not in statuses
        )
    }


def audit_strategy_run(
    run_root: str | Path,
    config: Mapping[str, Any],
) -> StrategyValidityAudit:
    """Audit the frozen matrix and the separate outer publication gate."""

    root = Path(run_root).resolve(strict=False)
    expected = expected_frozen_candidates(config)
    candidate_root = root / "sweep" / "candidates"
    actual_paths = {
        path.stem: path for path in candidate_root.glob("*/*.json")
    }
    unexpected = sorted(set(actual_paths) - set(expected))
    if unexpected:
        raise ValidityAuditError(
            "unexpected candidate IDs: " + ", ".join(unexpected)
        )
    rows: list[CandidateValidityRow] = []
    for config_id, candidate in sorted(
        expected.items(),
        key=lambda item: (item[1].stage_id, item[0]),
    ):
        path = actual_paths.get(config_id)
        if path is None:
            envelope: object = {}
        else:
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                envelope = {}
        rows.append(
            audit_candidate_envelope(
                envelope,
                expected_candidate=candidate,
            )
        )

    class_counts = Counter(row.validity_class for row in rows)
    stage_counts = Counter(row.stage_id for row in rows)
    try:
        state = json.loads(
            (root / "run-state.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        state = {}
    historical_task_counts = _outer_task_counts(state)
    task_counts = historical_task_counts
    reconciliation_status = "missing-or-invalid"
    try:
        reconciliation = load_rq2_reconciliation(root)
    except (ReconciliationContractError, OSError):
        reconciliation = None
    if reconciliation is not None:
        effective = reconciliation.get("effective_task_counts")
        if isinstance(effective, Mapping):
            task_counts = {
                status: int(effective.get(status, 0))
                for status in (
                    "pending",
                    "running",
                    "completed",
                    "failed",
                    "blocked",
                )
            } | {"unknown": 0}
            reconciliation_status = "completed"
    baseline_closed = (
        len(rows) == 35
        and dict(stage_counts) == EXPECTED_STAGE_COUNTS
        and class_counts["invalid-false-score"] == 0
        and class_counts["infrastructure/unknown"] == 0
    )
    blockers: list[str] = []
    if not baseline_closed:
        blockers.append("candidate-validity-gate-open")
    if any(
        task_counts.get(status, 0)
        for status in ("pending", "running", "failed", "blocked", "unknown")
    ):
        blockers.append("outer-task-completion-gate-open")
    return StrategyValidityAudit(
        run_id=root.name,
        rows=tuple(rows),
        classification_counts={
            name: class_counts[name] for name in VALIDITY_CLASSES
        },
        stage_counts={
            stage: stage_counts[stage] for stage in EXPECTED_STAGE_COUNTS
        },
        outer_task_counts=dict(task_counts),
        historical_outer_task_counts=dict(historical_task_counts),
        reconciliation_status=reconciliation_status,
        baseline_validity_gate_closed=baseline_closed,
        public_export_ready=not blockers,
        public_export_blockers=tuple(blockers),
    )


def write_audit_csv(
    path: str | Path,
    rows: Sequence[CandidateValidityRow],
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(CandidateValidityRow.__dataclass_fields__)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(row.to_dict() for row in rows)
    return destination


def write_audit_json(
    path: str | Path,
    audit: StrategyValidityAudit,
) -> Path:
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            audit.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
            allow_nan=False,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination
