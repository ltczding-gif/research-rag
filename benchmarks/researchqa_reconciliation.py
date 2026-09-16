"""Build the final, hash-bound rq-2 reconciliation and aggregate reports."""

from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from benchmarks.overnight import (
    canonical_json_bytes,
    fingerprint_payload,
    sha256_path,
)
from benchmarks.researchqa_reconciliation_contract import (
    APPROVED_EXTENSION_IDS,
    EXPECTED_BASELINE_CLASS_COUNTS,
    EXPECTED_EFFECTIVE_TASK_IDS,
    RECONCILIATION_ARTIFACT_PATHS,
    RECONCILIATION_REVISION,
    RECONCILIATION_SCHEMA_VERSION,
    load_rq2_reconciliation,
    reconciliation_code_fingerprint,
    reconciliation_path,
)
from benchmarks.researchqa_runtime import approved_extension_candidates
from benchmarks.researchqa_scoring import paired_bootstrap
from benchmarks.researchqa_strategy import generate_n1_candidate
from benchmarks.researchqa_sweep import (
    CANDIDATE_PROGRESS_SCHEMA_VERSION,
    SWEEP_ENGINE_REVISION,
    SWEEP_SCHEMA_VERSION,
)
from benchmarks.researchqa_validity_audit import (
    CandidateValidityRow,
    audit_candidate_envelope,
    audit_strategy_run,
)


FINAL_METRIC = "coverage_ndcg_at_10"
FINAL_BOOTSTRAP_SEED = "research-rag-rq2-final-bootstrap-v1"
METRIC_FIELDS = (
    "all_required_groups_success_at_10",
    "all_required_groups_success_at_5",
    "coverage_ndcg_at_10",
    "groups_covered_at_10",
    "groups_covered_at_5",
    "mrr",
    "recall_at_10",
    "recall_at_5",
)
LEADERBOARD_FIELDS = (
    "experiment_family",
    "extension_id",
    "stage_id",
    "stage_rank",
    "config_id",
    "status",
    "validity_class",
    "rankable",
    "mapping_passed",
    "guardrails_passed",
    "primary_metric",
    "primary_score",
    "baseline_config_id",
    "primary_delta",
    "new_hard_failure_count",
    "p95_latency_ms",
    "latency_validity",
    "index_bytes",
    "chunk_count",
    "pdf_chunker",
    "note_chunker",
    "retriever",
    "source_composition",
    "reranker",
)
BREAKDOWN_FIELDS = (
    "role",
    "config_id",
    "scope",
    "key",
    "domain",
    *METRIC_FIELDS,
)
_DIAGNOSTIC_KEYS = {
    "F2": "pdf_chunking",
    "RR1": "rerank_fusion",
    "R1": "retriever_fusion",
    "S1": "source_fusion",
}


class ResearchQAReconciliationError(RuntimeError):
    """Raised before an incomplete result can become a final rq-2 report."""


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchQAReconciliationError(f"{label} is unreadable") from exc
    if not isinstance(value, Mapping):
        raise ResearchQAReconciliationError(f"{label} must be a mapping")
    return value


def _atomic_write_bytes(path: Path, value: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(raw_temp_path)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def _atomic_write_json(path: Path, value: object) -> Path:
    return _atomic_write_bytes(path, canonical_json_bytes(value))


def _csv_bytes(
    fields: Sequence[str],
    rows: Sequence[Mapping[str, object]],
) -> bytes:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(
        {field: row.get(field, "") for field in fields}
        for row in rows
    )
    return buffer.getvalue().encode("utf-8")


def _config_fingerprint(config: Mapping[str, Any]) -> str:
    return fingerprint_payload(config)


def _data_inputs_fingerprint(root: Path) -> str:
    source_rows = []
    for path in sorted((root / "source").glob("W*/source-manifest.jsonl")):
        _size, digest = sha256_path(path)
        source_rows.append([path.parent.name, digest])
    if len(source_rows) != 20:
        raise ResearchQAReconciliationError(
            "data fingerprint requires 20 source manifests"
        )
    question_path = (
        root.parent.parent / "suites" / "rq-2" / "questions.jsonl"
    )
    _size, question_sha = sha256_path(question_path)
    frozen_path = root / "note-runs" / "frozen" / "frozen-notes.jsonl"
    try:
        frozen_rows = [
            json.loads(line)
            for line in frozen_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ResearchQAReconciliationError(
            "frozen-note manifest is unreadable"
        ) from exc
    if len(frozen_rows) != 20:
        raise ResearchQAReconciliationError(
            "data fingerprint requires 20 frozen notes"
        )
    note_hashes = []
    frozen_root = frozen_path.parent
    for row in frozen_rows:
        if not isinstance(row, Mapping):
            raise ResearchQAReconciliationError(
                "frozen-note manifest row is invalid"
            )
        paper_id = row.get("paper_id")
        expected = row.get("note_sha256")
        if (
            not isinstance(paper_id, str)
            or not paper_id
            or not isinstance(expected, str)
            or len(expected) != 64
        ):
            raise ResearchQAReconciliationError(
                "frozen-note identity is invalid"
            )
        _size, actual = sha256_path(
            frozen_root / "notes" / f"{paper_id}.md"
        )
        if actual != expected:
            raise ResearchQAReconciliationError(
                f"frozen-note hash mismatch: {paper_id}"
            )
        note_hashes.append([paper_id, expected])
    return fingerprint_payload(
        {
            "source_manifests": source_rows,
            "questions_jsonl": question_sha,
            "frozen_notes": sorted(note_hashes),
        }
    )


def _score_summary(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    value = payload.get("score_summary")
    if not isinstance(value, Mapping):
        raise ResearchQAReconciliationError(
            "completed candidate lacks score_summary"
        )
    return value


def _overall_metric(
    payload: Mapping[str, Any],
    metric: str = FINAL_METRIC,
) -> float:
    summary = _score_summary(payload)
    overall = summary.get("overall")
    if not isinstance(overall, Mapping):
        raise ResearchQAReconciliationError(
            "completed candidate lacks overall score summary"
        )
    value = overall.get(metric)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ResearchQAReconciliationError(
            f"completed candidate lacks finite {metric}"
        )
    number = float(value)
    if not (-float("inf") < number < float("inf")):
        raise ResearchQAReconciliationError(
            f"completed candidate has non-finite {metric}"
        )
    return number


def _candidate_path(root: Path, stage_id: str, config_id: str) -> Path:
    baseline = root / "sweep" / "candidates" / stage_id / f"{config_id}.json"
    if baseline.is_file():
        return baseline
    matches = list(
        (root / "sweep" / "extensions").glob(
            f"*/candidates/{stage_id}/{config_id}.json"
        )
    )
    if len(matches) != 1:
        raise ResearchQAReconciliationError(
            f"candidate path is not unique: {config_id}"
        )
    return matches[0]


def _finalized_progress(
    *,
    root: Path,
    candidate: object,
    input_fingerprint: str,
) -> tuple[Mapping[str, Any], Mapping[str, Any], str]:
    path = (
        root
        / "sweep"
        / "progress"
        / candidate.stage_id
        / candidate.config_id
        / f"{input_fingerprint}.json"
    )
    progress = _read_json(path, f"{candidate.config_id} progress envelope")
    payload = progress.get("payload")
    payload = payload if isinstance(payload, Mapping) else {}
    execution = payload.get("execution")
    execution = execution if isinstance(execution, Mapping) else {}
    question_results = execution.get("question_results")
    code_fingerprint = payload.get("code_fingerprint")
    if (
        progress.get("schema_version") != SWEEP_SCHEMA_VERSION
        or progress.get("engine_revision") != SWEEP_ENGINE_REVISION
        or progress.get("progress_schema_version")
        != CANDIDATE_PROGRESS_SCHEMA_VERSION
        or progress.get("config_id") != candidate.config_id
        or progress.get("stage_id") != candidate.stage_id
        or progress.get("input_fingerprint") != input_fingerprint
        or progress.get("payload_sha256") != fingerprint_payload(payload)
        or payload.get("candidate") != candidate.to_dict()
        or not isinstance(code_fingerprint, str)
        or len(code_fingerprint) != 64
        or any(
            character not in "0123456789abcdef"
            for character in code_fingerprint
        )
        or execution.get("progress_schema_version")
        != CANDIDATE_PROGRESS_SCHEMA_VERSION
        or not isinstance(question_results, list)
        or payload.get("question_results_sha256")
        != fingerprint_payload(question_results)
        or payload.get("finalized") is not True
        or execution.get("phase") != "finalized"
        or len(execution.get("completed_paper_ids", [])) != 20
        or len(execution.get("completed_question_ids", [])) != 254
    ):
        raise ResearchQAReconciliationError(
            f"finalized progress contract failed: {candidate.config_id}"
        )
    final_candidate = payload.get("final_candidate")
    if not isinstance(final_candidate, Mapping):
        raise ResearchQAReconciliationError(
            f"finalized progress lacks candidate: {candidate.config_id}"
        )
    relative = final_candidate.get("path")
    if not isinstance(relative, str) or not relative:
        raise ResearchQAReconciliationError(
            f"finalized candidate path is invalid: {candidate.config_id}"
        )
    candidate_path = (root / relative).resolve(strict=False)
    if root != candidate_path and root not in candidate_path.parents:
        raise ResearchQAReconciliationError(
            f"finalized candidate path escapes run: {candidate.config_id}"
        )
    candidate_envelope = _read_json(
        candidate_path,
        f"{candidate.config_id} finalized candidate",
    )
    candidate_payload = candidate_envelope.get("payload")
    candidate_payload = (
        candidate_payload
        if isinstance(candidate_payload, Mapping)
        else {}
    )
    _size, actual_sha = sha256_path(candidate_path)
    if (
        actual_sha != final_candidate.get("sha256")
        or candidate_envelope.get("payload_sha256")
        != fingerprint_payload(candidate_payload)
        or candidate_envelope.get("payload_sha256")
        != final_candidate.get("payload_sha256")
        or candidate_envelope.get("config_id") != candidate.config_id
        or candidate_envelope.get("stage_id") != candidate.stage_id
        or candidate_envelope.get("input_fingerprint")
        != input_fingerprint
        or candidate_envelope.get("status") != "completed"
        or candidate_payload.get("execution_complete") is not True
        or candidate_payload.get("guardrail_finalized") is not True
    ):
        raise ResearchQAReconciliationError(
            f"finalized progress differs from candidate: {candidate.config_id}"
        )
    return progress, execution, code_fingerprint


def _extension_row(
    *,
    root: Path,
    extension_id: str,
    expected_candidate: object,
    expected_baseline: object,
) -> tuple[dict[str, object], Mapping[str, Any], Mapping[str, Any]]:
    candidate = expected_candidate
    baseline = expected_baseline
    extension_root = root / "sweep" / "extensions" / extension_id
    candidate_path = (
        extension_root
        / "candidates"
        / candidate.stage_id
        / f"{candidate.config_id}.json"
    )
    envelope = _read_json(
        candidate_path,
        f"{extension_id} candidate envelope",
    )
    audit = audit_candidate_envelope(
        envelope,
        expected_candidate=candidate,
    )
    if (
        audit.status != "completed"
        or audit.validity_class
        not in {"valid-and-rankable", "valid-but-poor"}
        or audit.contract_errors
    ):
        raise ResearchQAReconciliationError(
            f"{extension_id} candidate is not a valid terminal result"
        )
    payload = envelope.get("payload")
    if not isinstance(payload, Mapping):
        raise ResearchQAReconciliationError(
            f"{extension_id} candidate payload is missing"
        )

    runtime = _read_json(
        extension_root / "runtime" / "runtime-summary.json",
        f"{extension_id} runtime summary",
    )
    prequality = _read_json(
        extension_root / "runtime" / "prequality.json",
        f"{extension_id} pre-quality",
    )
    preflight = _read_json(
        extension_root / "runtime" / "model-preflight.json",
        f"{extension_id} model preflight",
    )
    result = runtime.get("result")
    result = result if isinstance(result, Mapping) else {}
    lifecycle = runtime.get("lifecycle")
    lifecycle = lifecycle if isinstance(lifecycle, Mapping) else {}
    if (
        runtime.get("status") != "completed"
        or runtime.get("extension_id") != extension_id
        or runtime.get("candidate") != candidate.to_dict()
        or runtime.get("baseline") != baseline.to_dict()
        or result.get("status") != "completed"
        or result.get("completed_paper_count") != 20
        or result.get("completed_question_count") != 254
        or result.get("evaluable_question_count") != 239
        or result.get("mapped_group_count") != 380
        or lifecycle.get("embedding_released") is not True
        or (
            extension_id == "RR1"
            and (
                lifecycle.get("reranker_required") is not True
                or lifecycle.get("reranker_released") is not True
            )
        )
        or prequality.get("status") != "completed"
        or prequality.get("extension_id") != extension_id
        or prequality.get("candidate_config_id") != candidate.config_id
        or preflight.get("status") != "completed"
        or preflight.get("extension_id") != extension_id
    ):
        raise ResearchQAReconciliationError(
            f"{extension_id} runtime lifecycle or coverage is incomplete"
        )
    embedding = preflight.get("embedding")
    if (
        not isinstance(embedding, Mapping)
        or embedding.get("dimensions") != 2560
        or not isinstance(embedding.get("fingerprint"), str)
    ):
        raise ResearchQAReconciliationError(
            f"{extension_id} embedding preflight is invalid"
        )

    corpus = payload.get("corpus_diagnostics")
    corpus = corpus if isinstance(corpus, Mapping) else {}
    diagnostic_key = _DIAGNOSTIC_KEYS[extension_id]
    if corpus.get(diagnostic_key) != prequality.get("diagnostics"):
        raise ResearchQAReconciliationError(
            f"{extension_id} pre-quality differs from final diagnostics"
        )
    if (
        extension_id == "S1"
        and corpus.get("note_route") != prequality.get("note_route")
    ):
        raise ResearchQAReconciliationError(
            "S1 note-route diagnostics differ from pre-quality"
        )

    input_fingerprint = envelope.get("input_fingerprint")
    if not isinstance(input_fingerprint, str):
        raise ResearchQAReconciliationError(
            f"{extension_id} input fingerprint is invalid"
        )
    progress, execution, code_fingerprint = _finalized_progress(
        root=root,
        candidate=candidate,
        input_fingerprint=input_fingerprint,
    )
    if (
        not isinstance(execution, Mapping)
        or execution.get("phase") != "finalized"
    ):
        raise ResearchQAReconciliationError(
            f"{extension_id} progress is not finalized"
        )
    progress_payload = progress.get("payload")
    if not isinstance(progress_payload, Mapping):
        raise ResearchQAReconciliationError(
            f"{extension_id} progress payload is missing"
        )

    baseline_envelope = _read_json(
        _candidate_path(root, baseline.stage_id, baseline.config_id),
        f"{extension_id} baseline envelope",
    )
    baseline_payload = baseline_envelope.get("payload")
    if not isinstance(baseline_payload, Mapping):
        raise ResearchQAReconciliationError(
            f"{extension_id} baseline payload is missing"
        )
    primary = _overall_metric(payload)
    baseline_primary = _overall_metric(baseline_payload)
    diagnostics = payload.get("guardrail_diagnostics")
    diagnostics = diagnostics if isinstance(diagnostics, Mapping) else {}
    hard_failures = diagnostics.get("new_recall_at_10_hard_failure_ids")
    hard_failures = hard_failures if isinstance(hard_failures, list) else []
    row = {
        "extension_id": extension_id,
        "config_id": candidate.config_id,
        "stage_id": candidate.stage_id,
        "status": "completed",
        "validity_class": audit.validity_class,
        "rankable": candidate.rankable,
        "mapping_passed": True,
        "guardrails_passed": audit.guardrails_passed,
        "contract_errors": [],
        "baseline_config_id": baseline.config_id,
        "primary_metric": FINAL_METRIC,
        "primary_score": primary,
        "baseline_primary_score": baseline_primary,
        "primary_delta": primary - baseline_primary,
        "new_hard_failure_count": len(hard_failures),
        "p95_latency_ms": float(payload.get("p95_latency_ms", 0.0)),
        "latency_validity": str(
            (
                payload.get("latency_metrics")
                if isinstance(payload.get("latency_metrics"), Mapping)
                else {}
            ).get("validity")
            or "observed-only"
        ),
        "index_bytes": int(payload.get("index_bytes", 0)),
        "chunk_count": int(payload.get("chunk_count", 0)),
        "input_fingerprint": input_fingerprint,
        "payload_sha256": str(envelope.get("payload_sha256")),
        "progress_payload_sha256": str(progress.get("payload_sha256")),
        "code_fingerprint": code_fingerprint,
        "candidate_file_sha256": sha256_path(candidate_path)[1],
        "progress_file_sha256": sha256_path(
            (
                root
                / "sweep"
                / "progress"
                / candidate.stage_id
                / candidate.config_id
                / f"{input_fingerprint}.json"
            )
        )[1],
        "runtime_summary_sha256": sha256_path(
            extension_root / "runtime" / "runtime-summary.json"
        )[1],
        "prequality_sha256": sha256_path(
            extension_root / "runtime" / "prequality.json"
        )[1],
        "model_preflight_sha256": sha256_path(
            extension_root / "runtime" / "model-preflight.json"
        )[1],
        "candidate": candidate.to_dict(),
    }
    return row, payload, preflight


def _baseline_rows(
    root: Path,
    audit_rows: Sequence[CandidateValidityRow],
) -> tuple[list[dict[str, object]], dict[str, Mapping[str, Any]]]:
    rows = []
    payloads: dict[str, Mapping[str, Any]] = {}
    for audit in audit_rows:
        path = _candidate_path(root, audit.stage_id, audit.config_id)
        envelope = _read_json(path, f"baseline candidate {audit.config_id}")
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            raise ResearchQAReconciliationError(
                f"baseline payload is missing: {audit.config_id}"
            )
        candidate = payload.get("candidate")
        candidate = candidate if isinstance(candidate, Mapping) else {}
        mapping = payload.get("mapping")
        mapping = mapping if isinstance(mapping, Mapping) else {}
        coverage = mapping.get("coverage")
        coverage = coverage if isinstance(coverage, Mapping) else {}
        completed = audit.status == "completed"
        primary = _overall_metric(payload) if completed else None
        rows.append(
            {
                "experiment_family": "frozen-matrix",
                "extension_id": "",
                "stage_id": audit.stage_id,
                "stage_rank": "",
                "config_id": audit.config_id,
                "status": audit.status,
                "validity_class": audit.validity_class,
                "rankable": audit.rankable,
                "mapping_passed": (
                    coverage.get("passed") is True if completed else False
                ),
                "guardrails_passed": audit.guardrails_passed,
                "primary_metric": FINAL_METRIC,
                "primary_score": primary if primary is not None else "",
                "baseline_config_id": "",
                "primary_delta": "",
                "new_hard_failure_count": audit.new_hard_failure_count,
                "p95_latency_ms": (
                    float(payload.get("p95_latency_ms", 0.0))
                    if completed
                    else ""
                ),
                "latency_validity": audit.latency_validity,
                "index_bytes": (
                    int(payload.get("index_bytes", 0)) if completed else ""
                ),
                "chunk_count": (
                    int(payload.get("chunk_count", 0)) if completed else ""
                ),
                "pdf_chunker": candidate.get("pdf_chunker", ""),
                "note_chunker": candidate.get("note_chunker") or "",
                "retriever": candidate.get("retriever", ""),
                "source_composition": candidate.get(
                    "source_composition",
                    "",
                ),
                "reranker": candidate.get("reranker", ""),
            }
        )
        payloads[audit.config_id] = payload
    return rows, payloads


def _extension_leaderboard_row(
    row: Mapping[str, object],
) -> dict[str, object]:
    candidate = row["candidate"]
    candidate = candidate if isinstance(candidate, Mapping) else {}
    return {
        "experiment_family": "approved-extension",
        "extension_id": row["extension_id"],
        "stage_id": row["stage_id"],
        "stage_rank": "",
        "config_id": row["config_id"],
        "status": row["status"],
        "validity_class": row["validity_class"],
        "rankable": row["rankable"],
        "mapping_passed": row["mapping_passed"],
        "guardrails_passed": row["guardrails_passed"],
        "primary_metric": FINAL_METRIC,
        "primary_score": row["primary_score"],
        "baseline_config_id": row["baseline_config_id"],
        "primary_delta": row["primary_delta"],
        "new_hard_failure_count": row["new_hard_failure_count"],
        "p95_latency_ms": row["p95_latency_ms"],
        "latency_validity": row["latency_validity"],
        "index_bytes": row["index_bytes"],
        "chunk_count": row["chunk_count"],
        "pdf_chunker": candidate.get("pdf_chunker", ""),
        "note_chunker": candidate.get("note_chunker") or "",
        "retriever": candidate.get("retriever", ""),
        "source_composition": candidate.get("source_composition", ""),
        "reranker": candidate.get("reranker", ""),
    }


def _rank_rows(rows: list[dict[str, object]]) -> None:
    by_stage: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        if (
            row["status"] == "completed"
            and row["validity_class"] == "valid-and-rankable"
        ):
            by_stage[str(row["stage_id"])].append(row)
    for stage_rows in by_stage.values():
        ordered = sorted(
            stage_rows,
            key=lambda row: (
                -float(row["primary_score"]),
                int(row["index_bytes"]),
                str(row["config_id"]),
            ),
        )
        for rank, row in enumerate(ordered, 1):
            row["stage_rank"] = rank


def _pareto_rows(
    rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    points = [
        row
        for row in rows
        if (
            row.get("status") == "completed"
            and row.get("validity_class") == "valid-and-rankable"
        )
    ]
    use_latency = bool(points) and all(
        row.get("latency_validity") == "decisive" for row in points
    )
    frontier = []
    for point in points:
        dominated = any(
            other["config_id"] != point["config_id"]
            and float(other["primary_score"])
            >= float(point["primary_score"])
            and (
                not use_latency
                or float(other["p95_latency_ms"])
                <= float(point["p95_latency_ms"])
            )
            and int(other["index_bytes"]) <= int(point["index_bytes"])
            and (
                float(other["primary_score"])
                > float(point["primary_score"])
                or (
                    use_latency
                    and float(other["p95_latency_ms"])
                    < float(point["p95_latency_ms"])
                )
                or int(other["index_bytes"]) < int(point["index_bytes"])
            )
            for other in points
        )
        if not dominated:
            frontier.append(point)
    ordered = sorted(
        frontier,
        key=lambda row: (
            -float(row["primary_score"]),
            (
                float(row["p95_latency_ms"])
                if use_latency
                else 0.0
            ),
            int(row["index_bytes"]),
            str(row["config_id"]),
        ),
    )
    return [
        {
            "rank": rank,
            "config_id": row["config_id"],
            "stage_id": row["stage_id"],
            "primary": row["primary_score"],
            "p95_latency_ms": row["p95_latency_ms"],
            "index_bytes": row["index_bytes"],
            "chunk_count": row["chunk_count"],
            "status": row["status"],
            "guardrails_passed": row["guardrails_passed"],
        }
        for rank, row in enumerate(ordered, 1)
    ]


def _paper_domains(payload: Mapping[str, Any]) -> Mapping[str, str]:
    domains: dict[str, set[str]] = defaultdict(set)
    rows = payload.get("question_results")
    if not isinstance(rows, list):
        raise ResearchQAReconciliationError(
            "candidate question results are missing"
        )
    for row in rows:
        if not isinstance(row, Mapping):
            raise ResearchQAReconciliationError(
                "candidate question result is invalid"
            )
        domains[str(row.get("paper_id"))].add(str(row.get("domain")))
    if len(domains) != 20 or any(len(values) != 1 for values in domains.values()):
        raise ResearchQAReconciliationError(
            "paper/domain mapping is incomplete"
        )
    return {
        paper_id: next(iter(values))
        for paper_id, values in domains.items()
    }


def _bootstrap_payload(
    *,
    winner_id: str,
    winner_payload: Mapping[str, Any],
    baseline_id: str,
    baseline_payload: Mapping[str, Any],
) -> dict[str, object]:
    winner_by_paper = _score_summary(winner_payload).get("by_paper")
    baseline_by_paper = _score_summary(baseline_payload).get("by_paper")
    if not isinstance(winner_by_paper, Mapping) or not isinstance(
        baseline_by_paper,
        Mapping,
    ):
        raise ResearchQAReconciliationError(
            "winner/baseline paper scores are missing"
        )
    candidate_scores = {
        str(paper_id): float(
            metrics[FINAL_METRIC]
            if isinstance(metrics, Mapping)
            else float("nan")
        )
        for paper_id, metrics in winner_by_paper.items()
    }
    baseline_scores = {
        str(paper_id): float(
            metrics[FINAL_METRIC]
            if isinstance(metrics, Mapping)
            else float("nan")
        )
        for paper_id, metrics in baseline_by_paper.items()
    }
    result = paired_bootstrap(
        candidate_scores,
        baseline_scores,
        _paper_domains(winner_payload),
        samples=10_000,
        confidence=0.95,
        seed=FINAL_BOOTSTRAP_SEED,
    )
    return {
        "schema_version": 1,
        "metric": FINAL_METRIC,
        "candidate_config_id": winner_id,
        "baseline_config_id": baseline_id,
        **result.to_dict(),
    }


def _breakdown_rows(
    *,
    role: str,
    config_id: str,
    payload: Mapping[str, Any],
) -> list[dict[str, object]]:
    summary = _score_summary(payload)
    domains_by_paper = _paper_domains(payload)
    rows: list[dict[str, object]] = []
    for scope, source in (
        ("paper", summary.get("by_paper")),
        ("domain", summary.get("by_domain")),
        ("question_type", summary.get("by_question_type")),
    ):
        if not isinstance(source, Mapping):
            raise ResearchQAReconciliationError(
                f"{config_id} lacks {scope} breakdown"
            )
        for key, metrics in sorted(source.items()):
            if not isinstance(metrics, Mapping):
                raise ResearchQAReconciliationError(
                    f"{config_id} has invalid {scope} breakdown"
                )
            row = {
                "role": role,
                "config_id": config_id,
                "scope": scope,
                "key": key,
                "domain": (
                    domains_by_paper[str(key)]
                    if scope == "paper"
                    else key if scope == "domain" else ""
                ),
            }
            row.update({metric: metrics.get(metric, "") for metric in METRIC_FIELDS})
            rows.append(row)
    overall = summary.get("overall")
    if not isinstance(overall, Mapping):
        raise ResearchQAReconciliationError(
            f"{config_id} lacks overall breakdown"
        )
    rows.append(
        {
            "role": role,
            "config_id": config_id,
            "scope": "overall",
            "key": "all",
            "domain": "",
            **{metric: overall.get(metric, "") for metric in METRIC_FIELDS},
        }
    )
    return rows


def _note_prequality(root: Path, config: Mapping[str, Any]) -> dict[str, object]:
    path = (
        root
        / "sweep"
        / "extensions"
        / "N0-N3"
        / "runtime"
        / "prequality.json"
    )
    envelope = _read_json(path, "N0/N3 pre-quality")
    expected_candidate = generate_n1_candidate(config)
    diagnostics = envelope.get("diagnostics")
    if (
        envelope.get("schema_version") != 1
        or envelope.get("extension_id") != "N0-N3"
        or envelope.get("status") != "completed"
        or envelope.get("candidate") != expected_candidate.to_dict()
        or not isinstance(diagnostics, Mapping)
        or diagnostics.get("contract_status") != "passed"
        or diagnostics.get("paper_count") != 20
        or len(diagnostics.get("eligible_paper_ids", [])) != 20
        or diagnostics.get("fallback_paper_ids") != []
        or diagnostics.get("backlinkable_base_chunk_count")
        != diagnostics.get("base_chunk_count")
        or diagnostics.get("backlinkable_reviewer_chunk_count")
        != diagnostics.get("reviewer_chunk_count")
    ):
        raise ResearchQAReconciliationError(
            "N0/N3/N1 pre-quality contract is incomplete"
        )
    return {
        "status": "passed",
        "paper_count": 20,
        "eligible_paper_count": 20,
        "fallback_paper_count": 0,
        "base_chunk_count": int(diagnostics["base_chunk_count"]),
        "backlinkable_base_chunk_count": int(
            diagnostics["backlinkable_base_chunk_count"]
        ),
        "reviewer_chunk_count": int(diagnostics["reviewer_chunk_count"]),
        "backlinkable_reviewer_chunk_count": int(
            diagnostics["backlinkable_reviewer_chunk_count"]
        ),
        "diagnostic_fingerprint": str(
            diagnostics["diagnostic_fingerprint"]
        ),
        "artifact_sha256": sha256_path(path)[1],
    }


def reconcile_rq2_run(
    run_root: str | Path,
    config: Mapping[str, Any],
) -> Path:
    """Audit all terminal evidence and write deterministic superseding outputs."""

    root = Path(run_root).resolve(strict=True)
    state_path = root / "run-state.json"
    state = _read_json(state_path, "historical run state")
    tasks = state.get("tasks")
    tasks = tasks if isinstance(tasks, Mapping) else {}
    failed_task_ids = sorted(
        str(task_id)
        for task_id, task in tasks.items()
        if isinstance(task, Mapping) and task.get("status") == "failed"
    )
    completed_task_ids = sorted(
        str(task_id)
        for task_id, task in tasks.items()
        if isinstance(task, Mapping) and task.get("status") == "completed"
    )
    if (
        state.get("status") not in {"partial", "failed"}
        or not failed_task_ids
        or not completed_task_ids
    ):
        raise ResearchQAReconciliationError(
            "historical outer state is not the expected partial failure"
        )
    _state_size, state_sha = sha256_path(state_path)

    audit = audit_strategy_run(root, config)
    if (
        not audit.baseline_validity_gate_closed
        or dict(audit.classification_counts)
        != EXPECTED_BASELINE_CLASS_COUNTS
        or len(audit.rows) != 35
    ):
        raise ResearchQAReconciliationError(
            "frozen 35-candidate audit has not closed"
        )
    baseline_rows, baseline_payloads = _baseline_rows(root, audit.rows)

    pairs = approved_extension_candidates(config)
    if set(pairs) != APPROVED_EXTENSION_IDS:
        raise ResearchQAReconciliationError(
            "approved extension plan is incomplete"
        )
    extension_rows: list[dict[str, object]] = []
    extension_payloads: dict[str, Mapping[str, Any]] = {}
    extension_preflights: dict[str, Mapping[str, Any]] = {}
    for extension_id in ("F2", "RR1", "R1", "S1"):
        candidate, baseline = pairs[extension_id]
        row, payload, preflight = _extension_row(
            root=root,
            extension_id=extension_id,
            expected_candidate=candidate,
            expected_baseline=baseline,
        )
        extension_rows.append(row)
        extension_payloads[candidate.config_id] = payload
        extension_preflights[extension_id] = preflight

    note_prequality = _note_prequality(root, config)
    leaderboard_rows = baseline_rows + [
        _extension_leaderboard_row(row) for row in extension_rows
    ]
    _rank_rows(leaderboard_rows)
    leaderboard_rows.sort(
        key=lambda row: (
            0
            if row["validity_class"] == "valid-and-rankable"
            else 1,
            (
                -float(row["primary_score"])
                if row["primary_score"] != ""
                else float("inf")
            ),
            str(row["stage_id"]),
            str(row["config_id"]),
        )
    )
    eligible = [
        row
        for row in leaderboard_rows
        if row["validity_class"] == "valid-and-rankable"
    ]
    if not eligible:
        raise ResearchQAReconciliationError(
            "reconciliation has no valid rankable candidate"
        )
    winner_row = min(
        eligible,
        key=lambda row: (
            -float(row["primary_score"]),
            int(row["index_bytes"]),
            str(row["config_id"]),
        ),
    )
    winner_id = str(winner_row["config_id"])
    all_payloads = baseline_payloads | extension_payloads
    winner_payload = all_payloads[winner_id]
    winner_extension = next(
        (
            row
            for row in extension_rows
            if row["config_id"] == winner_id
        ),
        None,
    )
    if winner_extension is None:
        raise ResearchQAReconciliationError(
            "final rq-2 winner must come from an audited approved extension"
        )
    bootstrap_baseline_id = str(winner_extension["baseline_config_id"])
    bootstrap = _bootstrap_payload(
        winner_id=winner_id,
        winner_payload=winner_payload,
        baseline_id=bootstrap_baseline_id,
        baseline_payload=all_payloads[bootstrap_baseline_id],
    )
    pareto = _pareto_rows(leaderboard_rows)
    if winner_id not in {str(row["config_id"]) for row in pareto}:
        raise ResearchQAReconciliationError(
            "final winner is outside the eligible Pareto frontier"
        )

    final_root = root / "sweep" / "final"
    report_root = root / "report"
    decision = {
        "schema_version": 1,
        "revision": RECONCILIATION_REVISION,
        "winner_label": "provisional-benchmark-winner",
        "provisional_winner": winner_id,
        "winner_extension_id": winner_extension["extension_id"],
        "winner_candidate": winner_extension["candidate"],
        "primary_metric": FINAL_METRIC,
        "primary": winner_extension["primary_score"],
        "baseline_config_id": bootstrap_baseline_id,
        "primary_delta": winner_extension["primary_delta"],
        "guardrails_passed": True,
        "bootstrap": bootstrap,
        "baseline_classification_counts": dict(audit.classification_counts),
        "extension_classification_counts": dict(
            Counter(
                str(row["validity_class"]) for row in extension_rows
            )
        ),
        "stop_after_report": True,
        "rq5_started": False,
    }
    leaderboard_json = {
        "schema_version": 1,
        "revision": RECONCILIATION_REVISION,
        "metric": FINAL_METRIC,
        "candidate_count": len(leaderboard_rows),
        "rows": leaderboard_rows,
    }
    pareto_json = {
        "schema_version": 1,
        "revision": RECONCILIATION_REVISION,
        "latency_decision_mode": (
            "decisive"
            if all(row["latency_validity"] == "decisive" for row in eligible)
            else "observed-only-excluded-from-dominance"
        ),
        "rows": pareto,
    }

    breakdown_rows: list[dict[str, object]] = []
    breakdown_rows.extend(
        _breakdown_rows(
            role="winner-baseline",
            config_id=bootstrap_baseline_id,
            payload=all_payloads[bootstrap_baseline_id],
        )
    )
    for row in extension_rows:
        breakdown_rows.extend(
            _breakdown_rows(
                role=f"extension-{row['extension_id']}",
                config_id=str(row["config_id"]),
                payload=extension_payloads[str(row["config_id"])],
            )
        )
    blocked_rows = [
        {
            "kind": "candidate",
            "stage_id": row["stage_id"],
            "config_id": row["config_id"],
            "status": row["status"],
            "rankable": row["rankable"],
            "mapping_passed": row["mapping_passed"],
            "guardrails_passed": row["guardrails_passed"],
        }
        for row in leaderboard_rows
        if row["validity_class"] != "valid-and-rankable"
    ]

    artifact_values: dict[str, bytes] = {
        "sweep/final/superseding-decision-summary.json": (
            canonical_json_bytes(decision)
        ),
        "sweep/final/superseding-leaderboard.json": (
            canonical_json_bytes(leaderboard_json)
        ),
        "sweep/final/superseding-pareto-frontier.json": (
            canonical_json_bytes(pareto_json)
        ),
        "report/superseding-leaderboard.csv": _csv_bytes(
            LEADERBOARD_FIELDS,
            leaderboard_rows,
        ),
        "report/superseding-paper-domain-breakdown.csv": _csv_bytes(
            BREAKDOWN_FIELDS,
            breakdown_rows,
        ),
        "report/superseding-paired-bootstrap.json": (
            canonical_json_bytes(bootstrap)
        ),
        "report/superseding-blocked-and-unmapped.jsonl": b"".join(
            canonical_json_bytes(row) for row in blocked_rows
        ),
    }
    if set(artifact_values) != RECONCILIATION_ARTIFACT_PATHS:
        raise ResearchQAReconciliationError(
            "reconciliation artifact plan differs from the contract"
        )
    for relative, value in artifact_values.items():
        _atomic_write_bytes(root / relative, value)
    artifact_rows = []
    for relative in sorted(artifact_values):
        size, digest = sha256_path(root / relative)
        artifact_rows.append(
            {"path": relative, "bytes": size, "sha256": digest}
        )

    embedding_fingerprints = {
        str(
            (
                preflight.get("embedding")
                if isinstance(preflight.get("embedding"), Mapping)
                else {}
            ).get("fingerprint")
        )
        for preflight in extension_preflights.values()
    }
    if len(embedding_fingerprints) != 1:
        raise ResearchQAReconciliationError(
            "extensions used different embedding identities"
        )
    rr1_reranker = extension_preflights["RR1"].get("reranker")
    if not isinstance(rr1_reranker, Mapping) or not isinstance(
        rr1_reranker.get("fingerprint"),
        str,
    ):
        raise ResearchQAReconciliationError(
            "RR1 reranker identity is unavailable"
        )

    code_fingerprint = reconciliation_code_fingerprint()
    config_fingerprint = _config_fingerprint(config)
    effective_tasks = [
        {"task_id": task_id, "status": "completed"}
        for task_id in sorted(EXPECTED_EFFECTIVE_TASK_IDS)
    ]
    payload = {
        "status": "completed",
        "public_export_ready": True,
        "run_id": root.name,
        "revision": RECONCILIATION_REVISION,
        "fingerprints": {
            "code": code_fingerprint,
            "config": config_fingerprint,
            "embedding-model": next(iter(embedding_fingerprints)),
            "reranker-model": str(rr1_reranker["fingerprint"]),
            "data-inputs": _data_inputs_fingerprint(root),
        },
        "source_run_state": {
            "status": state["status"],
            "sha256": state_sha,
            "completed_task_ids": completed_task_ids,
            "superseded_failed_task_ids": failed_task_ids,
            "superseded_failure_count": len(failed_task_ids),
        },
        "baseline_audit": {
            "candidate_count": len(audit.rows),
            "validity_gate_closed": audit.baseline_validity_gate_closed,
            "classification_counts": dict(audit.classification_counts),
            "stage_counts": dict(audit.stage_counts),
            "rows_fingerprint": fingerprint_payload(
                [row.to_dict() for row in audit.rows]
            ),
            "candidate_artifacts": [
                {
                    "stage_id": row.stage_id,
                    "config_id": row.config_id,
                    "file_sha256": sha256_path(
                        (
                            root
                            / "sweep"
                            / "candidates"
                            / row.stage_id
                            / f"{row.config_id}.json"
                        )
                    )[1],
                }
                for row in audit.rows
            ],
        },
        "note_prequality": note_prequality,
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
            "provisional_winner": winner_id,
            "baseline_config_id": bootstrap_baseline_id,
            "primary_metric": FINAL_METRIC,
            "primary_score": winner_extension["primary_score"],
            "primary_delta": winner_extension["primary_delta"],
            "guardrails_passed": True,
            "pareto_config_ids": [
                row["config_id"] for row in pareto
            ],
            "stop_after_report": True,
            "rq5_started": False,
        },
        "artifacts": artifact_rows,
    }
    envelope = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "revision": RECONCILIATION_REVISION,
        "run_id": root.name,
        "payload_sha256": fingerprint_payload(payload),
        "payload": payload,
    }
    output_path = _atomic_write_json(reconciliation_path(root), envelope)
    load_rq2_reconciliation(
        root,
        expected_config_fingerprint=config_fingerprint,
    )
    return output_path


def load_config(path: str | Path) -> Mapping[str, Any]:
    try:
        value = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ResearchQAReconciliationError(
            "rq-2 configuration is unreadable"
        ) from exc
    if not isinstance(value, Mapping):
        raise ResearchQAReconciliationError(
            "rq-2 configuration must be a mapping"
        )
    return value


__all__ = [
    "BREAKDOWN_FIELDS",
    "FINAL_BOOTSTRAP_SEED",
    "FINAL_METRIC",
    "LEADERBOARD_FIELDS",
    "METRIC_FIELDS",
    "ResearchQAReconciliationError",
    "load_config",
    "reconcile_rq2_run",
]
