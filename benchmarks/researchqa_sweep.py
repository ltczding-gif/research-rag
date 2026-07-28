"""Recoverable Gate C-F strategy sweep for ResearchQA rq-2.

Each candidate is an independently hash-verified JSON checkpoint.  Stage
rankings are rebuilt from those checkpoints, so a process interruption never
requires recomputing completed candidates and failed/incomplete candidates can
never enter a leaderboard.
"""

from __future__ import annotations

import csv
import gc
import io
import json
import math
import os
import tempfile
import traceback
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from benchmarks.overnight import canonical_json_bytes, fingerprint_payload
from benchmarks.researchqa_models import ModelAdapterError, ModelTransportError
from benchmarks.researchqa_scoring import (
    CandidateSummary,
    paired_bootstrap,
    rank_candidates,
)
from benchmarks.researchqa_strategy import (
    CandidateRunResult,
    ConfirmationSelection,
    EmbedderAdapter,
    EvidenceMappingBundle,
    StrategyCandidate,
    StrategyContractError,
    generate_orthogonal_candidates,
    normalize_paper_id,
    run_complete_candidate,
)
from service.pdf_ir import CanonicalDocument


SWEEP_SCHEMA_VERSION = 1
SWEEP_ENGINE_REVISION = "researchqa-sweep-v9"


class SweepContractError(ValueError):
    """Raised when the sweep cannot preserve a comparable ranking contract."""


CandidateExecutor = Callable[..., CandidateRunResult]
BeforeRerankCallback = Callable[[], None]
CacheOnlyCallback = Callable[[StrategyCandidate], None]
GuardrailCheck = Callable[[CandidateRunResult], bool]


@dataclass(frozen=True)
class SweepCandidateRecord:
    candidate: StrategyCandidate
    status: str
    input_fingerprint: str
    payload: Mapping[str, Any]
    result_path: str
    resumed: bool = False

    @property
    def error(self) -> str | None:
        value = self.payload.get("error")
        return str(value) if value is not None else None

    @property
    def primary(self) -> float | None:
        value = self.payload.get("primary_score")
        return float(value) if value is not None else None

    @property
    def mapping_passed(self) -> bool:
        mapping = self.payload.get("mapping")
        return bool(
            isinstance(mapping, Mapping)
            and isinstance(mapping.get("coverage"), Mapping)
            and mapping["coverage"].get("passed")
        )

    @property
    def guardrails_passed(self) -> bool:
        return self.guardrail_finalized and bool(
            self.payload.get("guardrails_passed")
        )

    @property
    def guardrail_finalized(self) -> bool:
        explicit = self.payload.get("guardrail_finalized")
        if explicit is not None:
            return bool(explicit)
        return isinstance(
            self.payload.get("guardrail_diagnostics"),
            Mapping,
        )

    @property
    def failure_kind(self) -> str | None:
        if self.status != "failed":
            return None
        explicit = self.payload.get("failure_kind")
        if explicit in {"strategy", "infrastructure", "unknown"}:
            return str(explicit)
        error_type = str(self.payload.get("error_type", ""))
        error = str(self.payload.get("error", "")).lower()
        if error_type == "StrategyContractError":
            return "strategy"
        if error_type in {
            "AcceleratorError",
            "MemoryError",
            "ModelInferenceError",
            "ModelPreflightError",
            "SystemError",
        } or any(
            marker in error
            for marker in (
                "cuda",
                "out of memory",
                "error return without exception set",
                "illegal memory access",
            )
        ):
            return "infrastructure"
        return "unknown"

    @property
    def p95_latency_ms(self) -> float:
        return float(self.payload.get("p95_latency_ms", math.inf))

    @property
    def index_bytes(self) -> int:
        return int(self.payload.get("index_bytes", 0))

    @property
    def chunk_count(self) -> int:
        return int(self.payload.get("chunk_count", 0))

    @property
    def completed_paper_ids(self) -> tuple[str, ...]:
        values = self.payload.get("completed_paper_ids", ())
        return tuple(str(value) for value in values)

    @property
    def completed_question_ids(self) -> tuple[str, ...]:
        values = self.payload.get("completed_question_ids", ())
        return tuple(str(value) for value in values)

    @property
    def evaluable_set(self) -> frozenset[tuple[str, str]]:
        mapping = self.payload.get("mapping")
        if not isinstance(mapping, Mapping):
            return frozenset()
        compact_rows = mapping.get("evaluable_set")
        if isinstance(compact_rows, list):
            return frozenset(
                (str(row[0]), str(row[1]))
                for row in compact_rows
                if isinstance(row, list) and len(row) == 2
            )
        rows = mapping.get("mappings")
        if not isinstance(rows, list):
            return frozenset()
        return frozenset(
            (str(row.get("row_id")), str(group.get("group_id")))
            for row in rows
            if isinstance(row, Mapping)
            for group in row.get("groups", ())
            if isinstance(group, Mapping) and bool(group.get("mapped"))
        )

    def is_complete(
        self,
        *,
        expected_paper_ids: Sequence[str],
        expected_question_ids: Sequence[str],
    ) -> bool:
        return (
            self.status == "completed"
            and set(self.completed_paper_ids)
            == {normalize_paper_id(value) for value in expected_paper_ids}
            and set(self.completed_question_ids) == set(expected_question_ids)
        )

    def summary(
        self,
        *,
        expected_paper_ids: Sequence[str],
        expected_question_ids: Sequence[str],
    ) -> CandidateSummary:
        return CandidateSummary(
            config_id=self.candidate.config_id,
            primary=float(self.primary) if self.primary is not None else -math.inf,
            p95_latency_ms=self.p95_latency_ms,
            index_bytes=self.index_bytes,
            chunk_count=self.chunk_count,
            complete=self.is_complete(
                expected_paper_ids=expected_paper_ids,
                expected_question_ids=expected_question_ids,
            ),
            guardrails_passed=(
                self.mapping_passed
                and self.guardrails_passed
                and self.candidate.rankable
            ),
        )


@dataclass(frozen=True)
class SweepStageRanking:
    stage_id: str
    ranked: tuple[SweepCandidateRecord, ...]
    failed_config_ids: tuple[str, ...]
    incomplete_config_ids: tuple[str, ...]
    pending_guardrail_config_ids: tuple[str, ...]
    ineligible_config_ids: tuple[str, ...]
    evaluable_set_fingerprint: str | None
    status: str = "completed"
    error: str | None = None

    @property
    def top2(self) -> tuple[SweepCandidateRecord, ...]:
        return self.ranked[:2]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SWEEP_SCHEMA_VERSION,
            "stage_id": self.stage_id,
            "ranked_config_ids": [
                record.candidate.config_id for record in self.ranked
            ],
            "top2_config_ids": [
                record.candidate.config_id for record in self.top2
            ],
            "failed_config_ids": list(self.failed_config_ids),
            "incomplete_config_ids": list(self.incomplete_config_ids),
            "pending_guardrail_config_ids": list(
                self.pending_guardrail_config_ids
            ),
            "ineligible_config_ids": list(self.ineligible_config_ids),
            "evaluable_set_fingerprint": self.evaluable_set_fingerprint,
            "status": self.status,
            "error": self.error,
        }


@dataclass(frozen=True)
class StrategySweepResult:
    records: tuple[SweepCandidateRecord, ...]
    stage_rankings: Mapping[str, SweepStageRanking]
    provisional_winner: str
    leaderboard: tuple[Mapping[str, object], ...]
    pareto_frontier: tuple[Mapping[str, object], ...]
    artifact_paths: tuple[str, ...]


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> Path:
    return _atomic_write_bytes(path, canonical_json_bytes(value))


def _atomic_write_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def _atomic_write_csv(
    path: Path,
    rows: Sequence[Mapping[str, object]],
    *,
    fieldnames: Sequence[str],
) -> Path:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(
        buffer,
        fieldnames=list(fieldnames),
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return _atomic_write_bytes(path, buffer.getvalue().encode("utf-8"))


def _atomic_write_jsonl(
    path: Path,
    rows: Sequence[Mapping[str, object]],
) -> Path:
    payload = "".join(
        json.dumps(
            row,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
        for row in rows
    ).encode("utf-8")
    return _atomic_write_bytes(path, payload)


def _result_path(run_root: Path, candidate: StrategyCandidate) -> Path:
    return (
        run_root
        / "sweep"
        / "candidates"
        / candidate.stage_id
        / f"{candidate.config_id}.json"
    )


_COMPACT_PAYLOAD_KEYS = (
    "schema_version",
    "candidate",
    "chunk_count",
    "completed_paper_ids",
    "completed_question_ids",
    "error",
    "error_type",
    "execution_complete",
    "failure_context",
    "failure_kind",
    "guardrail_diagnostics",
    "guardrail_finalized",
    "guardrails_passed",
    "index_bytes",
    "latency_metrics",
    "metric_bundle_complete",
    "p95_latency_ms",
    "primary_metric",
    "primary_score",
    "retrieval_scope",
    "score_summary",
)


def _compact_candidate_payload(
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Keep ranking/report facts in memory; leave verbose rows on disk."""

    compact = {
        key: payload[key]
        for key in _COMPACT_PAYLOAD_KEYS
        if key in payload
    }
    mapping = payload.get("mapping")
    if isinstance(mapping, Mapping):
        raw_rows = mapping.get("mappings")
        evaluable_set = sorted(
            {
                (str(row.get("row_id")), str(group.get("group_id")))
                for row in raw_rows
                if isinstance(row, Mapping)
                for group in row.get("groups", ())
                if isinstance(group, Mapping) and bool(group.get("mapped"))
            }
        ) if isinstance(raw_rows, list) else []
        compact["mapping"] = {
            key: mapping[key]
            for key in (
                "schema_version",
                "revision",
                "fuzzy_threshold",
                "coverage",
                "unmapped",
            )
            if key in mapping
        }
        compact["mapping"]["evaluable_set"] = [
            list(item) for item in evaluable_set
        ]

    question_rows = payload.get("question_results")
    if isinstance(question_rows, list):
        paper_domains: dict[str, str] = {}
        for row in question_rows:
            if not isinstance(row, Mapping):
                raise SweepContractError("invalid question result payload")
            paper_id = str(row.get("paper_id") or "")
            domain = str(row.get("domain") or "")
            if not paper_id or not domain:
                raise SweepContractError(
                    "question result has a blank paper/domain"
                )
            previous = paper_domains.setdefault(paper_id, domain)
            if previous != domain:
                raise SweepContractError(
                    f"paper {paper_id} spans multiple domains"
                )
        compact["paper_domains"] = dict(sorted(paper_domains.items()))
    return compact


def _candidate_input_fingerprint(
    candidate: StrategyCandidate,
    *,
    config_fingerprint: str,
    documents: Mapping[str, CanonicalDocument],
    questions: Sequence[Mapping[str, Any]],
    notes: Mapping[str, str],
    embedder: EmbedderAdapter,
    reranker: object,
) -> str:
    document_identity = {
        paper_id: {
            "file_hash": document.file_hash,
            "extractor_fingerprint": document.extractor_fingerprint,
            "page_hashes": [
                page.page_text_hash for page in document.pages
            ],
        }
        for paper_id, document in sorted(documents.items())
    }
    note_identity = (
        {
            paper_id: fingerprint_payload(text)
            for paper_id, text in sorted(notes.items())
        }
        if candidate.requires_notes
        else {}
    )
    embedder_identity = {
        "adapter_type": (
            f"{type(embedder).__module__}.{type(embedder).__qualname__}"
        ),
        **{
            key: getattr(embedder, key)
            for key in (
                "model_id",
                "model_digest",
                "dimensions",
                "normalization_revision",
            )
            if getattr(embedder, key, None) is not None
        },
    }
    reranker_identity = (
        {
            "adapter_type": (
                f"{type(reranker).__module__}.{type(reranker).__qualname__}"
            ),
            **{
                key: getattr(reranker, key)
                for key in (
                    "model_id",
                    "revision",
                    "inference_dtype",
                    "adapter_revision",
                )
                if getattr(reranker, key, None) is not None
            },
        }
        if candidate.reranker != "rerank-off"
        else {}
    )
    return fingerprint_payload(
        {
            "engine_revision": SWEEP_ENGINE_REVISION,
            "config_fingerprint": config_fingerprint,
            "candidate": candidate.to_dict(),
            "documents": document_identity,
            "questions": list(questions),
            "notes": note_identity,
            "embedder": embedder_identity,
            "reranker": reranker_identity,
        }
    )


def _write_candidate_record(
    path: Path,
    *,
    candidate: StrategyCandidate,
    input_fingerprint: str,
    status: str,
    payload: Mapping[str, Any],
) -> SweepCandidateRecord:
    envelope = {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "engine_revision": SWEEP_ENGINE_REVISION,
        "config_id": candidate.config_id,
        "stage_id": candidate.stage_id,
        "input_fingerprint": input_fingerprint,
        "status": status,
        "payload_sha256": fingerprint_payload(payload),
        "payload": dict(payload),
    }
    _atomic_write_json(path, envelope)
    return SweepCandidateRecord(
        candidate=candidate,
        status=status,
        input_fingerprint=input_fingerprint,
        payload=_compact_candidate_payload(payload),
        result_path=str(path.resolve()),
    )


def _failure_kind(exc: Exception) -> str:
    if isinstance(exc, StrategyContractError):
        return "strategy"
    message = str(exc).lower()
    if isinstance(exc, (ModelAdapterError, MemoryError, SystemError)) or any(
        marker in message
        for marker in (
            "cuda",
            "out of memory",
            "error return without exception set",
            "illegal memory access",
        )
    ):
        return "infrastructure"
    return "unknown"


def _load_candidate_record(
    path: Path,
    *,
    candidate: StrategyCandidate,
    input_fingerprint: str,
) -> SweepCandidateRecord | None:
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(envelope, Mapping):
        return None
    payload = envelope.get("payload")
    status = envelope.get("status")
    if (
        envelope.get("schema_version") != SWEEP_SCHEMA_VERSION
        or envelope.get("engine_revision") != SWEEP_ENGINE_REVISION
        or envelope.get("config_id") != candidate.config_id
        or envelope.get("stage_id") != candidate.stage_id
        or envelope.get("input_fingerprint") != input_fingerprint
        or status not in {"completed", "incomplete", "failed"}
        or not isinstance(payload, Mapping)
        or envelope.get("payload_sha256") != fingerprint_payload(payload)
    ):
        return None
    return SweepCandidateRecord(
        candidate=candidate,
        status=str(status),
        input_fingerprint=input_fingerprint,
        payload=_compact_candidate_payload(payload),
        result_path=str(path.resolve()),
        resumed=True,
    )


def _read_full_candidate_payload(
    record: SweepCandidateRecord,
) -> dict[str, Any]:
    path = Path(record.result_path)
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SweepContractError(
            f"candidate checkpoint is unreadable: {record.candidate.config_id}"
        ) from exc
    payload = envelope.get("payload") if isinstance(envelope, Mapping) else None
    if (
        not isinstance(payload, Mapping)
        or envelope.get("payload_sha256") != fingerprint_payload(payload)
    ):
        raise SweepContractError(
            f"candidate checkpoint hash mismatch: {record.candidate.config_id}"
        )
    return dict(payload)


def _matching_baseline(
    stage_id: str,
    record: SweepCandidateRecord,
    records: Sequence[SweepCandidateRecord],
) -> SweepCandidateRecord:
    candidate = record.candidate
    if stage_id in {"reranker", "top2-confirmation"}:
        matches = [
            item
            for item in records
            if item.status == "completed"
            and item.candidate.reranker == "rerank-off"
            and item.candidate.pdf_chunker == candidate.pdf_chunker
            and item.candidate.note_chunker == candidate.note_chunker
            and item.candidate.retriever == candidate.retriever
            and item.candidate.source_composition
            == candidate.source_composition
        ]
    else:
        baseline_component = {
            "pdf-chunker": ("pdf_chunker", "pdf-fixed-800"),
            "note-chunker": ("note_chunker", "note-section"),
            "retriever": ("retriever", "dense"),
            "source-composition": (
                "source_composition",
                "pdf-only",
            ),
        }.get(stage_id)
        if baseline_component is None:
            raise SweepContractError(
                f"{stage_id}: no relative guardrail baseline contract"
            )
        attribute, expected = baseline_component
        matches = [
            item
            for item in records
            if item.status == "completed"
            and getattr(item.candidate, attribute) == expected
        ]
    if len(matches) != 1:
        raise SweepContractError(
            f"{record.candidate.config_id}: expected exactly one relative "
            f"guardrail baseline, found {len(matches)}"
        )
    return matches[0]


def _required_score_summary(
    payload: Mapping[str, Any],
    config_id: str,
) -> Mapping[str, Any]:
    value = payload.get("score_summary")
    if not isinstance(value, Mapping):
        raise SweepContractError(
            f"{config_id}: score_summary is missing"
        )
    return value


def _metric_value(
    metrics: object,
    metric: str,
) -> float | None:
    if not isinstance(metrics, Mapping):
        return None
    value = metrics.get(metric)
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _metric_delta(
    candidate_metrics: object,
    baseline_metrics: object,
    metric: str,
) -> float | None:
    candidate_value = _metric_value(candidate_metrics, metric)
    baseline_value = _metric_value(baseline_metrics, metric)
    if candidate_value is None or baseline_value is None:
        return None
    return candidate_value - baseline_value


def _new_recall_hard_failures(
    candidate_payload: Mapping[str, Any],
    baseline_payload: Mapping[str, Any],
) -> tuple[str, ...]:
    def recalls(payload: Mapping[str, Any]) -> dict[str, float]:
        rows = payload.get("question_results")
        if not isinstance(rows, list):
            raise SweepContractError(
                "completed candidate is missing question_results"
            )
        values: dict[str, float] = {}
        for row in rows:
            if not isinstance(row, Mapping):
                raise SweepContractError("invalid question result")
            row_id = str(row.get("row_id") or "")
            value = _metric_value(row.get("metrics"), "recall_at_10")
            if row_id and value is not None:
                values[row_id] = value
        return values

    candidate = recalls(candidate_payload)
    baseline = recalls(baseline_payload)
    return tuple(
        sorted(
            row_id
            for row_id, baseline_value in baseline.items()
            if baseline_value > 0.0
            and candidate.get(row_id) is not None
            and math.isclose(candidate[row_id], 0.0, abs_tol=1e-12)
        )
    )


def _percentile(
    values: Sequence[float],
    probability: float,
) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] + (
        ordered[upper] - ordered[lower]
    ) * fraction


def _unevaluable_adversarial_diagnostics(
    payload: Mapping[str, Any],
) -> Mapping[str, object]:
    rows = payload.get("question_results")
    if not isinstance(rows, list):
        return {
            "count": 0,
            "row_ids": [],
            "top1_score_distribution": None,
        }
    row_ids: list[str] = []
    top1_scores: list[float] = []
    for row in rows:
        if (
            not isinstance(row, Mapping)
            or row.get("question_type") != "adversarial"
            or _metric_value(row.get("metrics"), "recall_at_10") is not None
        ):
            continue
        row_ids.append(str(row.get("row_id") or ""))
        scores = row.get("ranked_scores")
        if isinstance(scores, list) and scores:
            score = float(scores[0])
            if math.isfinite(score):
                top1_scores.append(score)
    distribution = (
        {
            "minimum": min(top1_scores),
            "median": _percentile(top1_scores, 0.50),
            "p95": _percentile(top1_scores, 0.95),
            "maximum": max(top1_scores),
        }
        if top1_scores
        else None
    )
    return {
        "count": len(row_ids),
        "row_ids": sorted(row_ids),
        "top1_score_distribution": distribution,
        "interpretation": (
            "Retrieval-score diagnostic only; this is not refusal accuracy."
        ),
    }


def _relative_guardrail_diagnostics(
    record: SweepCandidateRecord,
    baseline: SweepCandidateRecord,
    payload: Mapping[str, Any],
    baseline_payload: Mapping[str, Any],
    *,
    max_domain_regression: float,
    max_regressed_domains: int,
    max_question_type_regression: float,
    max_overall_guardrail_regression: float,
    max_new_recall_at_10_hard_failures: int,
) -> dict[str, object]:
    candidate_summary = _required_score_summary(
        payload,
        record.candidate.config_id,
    )
    baseline_summary = _required_score_summary(
        baseline_payload,
        baseline.candidate.config_id,
    )
    primary_metric = str(payload.get("primary_metric") or "")
    baseline_primary = str(baseline_payload.get("primary_metric") or "")
    if not primary_metric or primary_metric != baseline_primary:
        raise SweepContractError(
            f"{record.candidate.config_id}: primary metric differs from baseline"
        )
    failures: list[str] = []
    metric_bundle_complete = bool(
        payload.get("metric_bundle_complete")
    )
    if not metric_bundle_complete:
        failures.append("metric-bundle-incomplete")

    candidate_domains = candidate_summary.get("by_domain")
    baseline_domains = baseline_summary.get("by_domain")
    if not isinstance(candidate_domains, Mapping) or not isinstance(
        baseline_domains,
        Mapping,
    ):
        raise SweepContractError(
            f"{record.candidate.config_id}: domain scores are missing"
        )
    domain_deltas = {
        str(domain): delta
        for domain in sorted(set(candidate_domains) & set(baseline_domains))
        if (
            delta := _metric_delta(
                candidate_domains[domain],
                baseline_domains[domain],
                primary_metric,
            )
        )
        is not None
    }
    regressed_domains = {
        domain: delta
        for domain, delta in domain_deltas.items()
        if delta < -max_domain_regression
    }
    if len(regressed_domains) > max_regressed_domains:
        failures.append("too-many-domain-regressions")

    candidate_types = candidate_summary.get("by_question_type")
    baseline_types = baseline_summary.get("by_question_type")
    if not isinstance(candidate_types, Mapping) or not isinstance(
        baseline_types,
        Mapping,
    ):
        raise SweepContractError(
            f"{record.candidate.config_id}: question-type scores are missing"
        )
    question_type_deltas = {
        question_type: _metric_delta(
            candidate_types.get(question_type),
            baseline_types.get(question_type),
            primary_metric,
        )
        for question_type in ("multi_hop", "adversarial")
    }
    for question_type, delta in question_type_deltas.items():
        if delta is not None and delta < -max_question_type_regression:
            failures.append(f"{question_type}-regression")

    candidate_overall = candidate_summary.get("overall")
    baseline_overall = baseline_summary.get("overall")
    overall_deltas = {
        metric: _metric_delta(
            candidate_overall,
            baseline_overall,
            metric,
        )
        for metric in (
            "recall_at_10",
            "all_required_groups_success_at_10",
        )
    }
    for metric, delta in overall_deltas.items():
        if (
            delta is not None
            and delta < -max_overall_guardrail_regression
        ):
            failures.append(f"{metric}-regression")

    new_hard_failures = _new_recall_hard_failures(
        payload,
        baseline_payload,
    )
    if len(new_hard_failures) > max_new_recall_at_10_hard_failures:
        failures.append("new-recall-at-10-hard-failures")

    latency_ratio = (
        float(payload.get("p95_latency_ms", math.inf))
        / max(float(baseline_payload.get("p95_latency_ms", math.inf)), 1e-12)
    )
    index_ratio = (
        int(payload.get("index_bytes", 0))
        / max(int(baseline_payload.get("index_bytes", 0)), 1)
    )
    return {
        "revision": "rq2-relative-regression-v1",
        "comparison_role": (
            "baseline"
            if record.candidate.config_id == baseline.candidate.config_id
            else "candidate"
        ),
        "baseline_config_id": baseline.candidate.config_id,
        "primary_metric": primary_metric,
        "primary_delta": _metric_delta(
            candidate_overall,
            baseline_overall,
            primary_metric,
        ),
        "domain_deltas": domain_deltas,
        "regressed_domains": regressed_domains,
        "question_type_deltas": question_type_deltas,
        "overall_guardrail_deltas": overall_deltas,
        "new_recall_at_10_hard_failure_ids": list(new_hard_failures),
        "p95_latency_ratio": latency_ratio,
        "index_size_ratio": index_ratio,
        "operational_review_required": (
            latency_ratio > 1.5 or index_ratio > 1.5
        ),
        "unevaluable_adversarial": (
            _unevaluable_adversarial_diagnostics(payload)
        ),
        "thresholds": {
            "max_domain_regression": max_domain_regression,
            "max_regressed_domains": max_regressed_domains,
            "max_question_type_regression": (
                max_question_type_regression
            ),
            "max_overall_guardrail_regression": (
                max_overall_guardrail_regression
            ),
            "max_new_recall_at_10_hard_failures": (
                max_new_recall_at_10_hard_failures
            ),
        },
        "failures": failures,
        "passed": not failures,
    }


def _apply_relative_guardrails(
    stage_id: str,
    records: Sequence[SweepCandidateRecord],
    gates: Mapping[str, Any],
    *,
    upstream_eligible_components: Mapping[str, frozenset[str]] | None = None,
) -> tuple[SweepCandidateRecord, ...]:
    policy = gates.get("relative_guardrails")
    if not isinstance(policy, Mapping):
        raise SweepContractError(
            "config.gates.relative_guardrails must be a mapping"
        )
    thresholds = {
        "max_domain_regression": float(
            policy.get("max_domain_regression", 0.02)
        ),
        "max_regressed_domains": int(
            policy.get("max_regressed_domains", 1)
        ),
        "max_question_type_regression": float(
            policy.get("max_question_type_regression", 0.02)
        ),
        "max_overall_guardrail_regression": float(
            policy.get("max_overall_guardrail_regression", 0.005)
        ),
        "max_new_recall_at_10_hard_failures": int(
            policy.get("max_new_recall_at_10_hard_failures", 0)
        ),
    }
    completed = [record for record in records if record.status == "completed"]
    full_payloads = {
        record.candidate.config_id: _read_full_candidate_payload(record)
        for record in completed
    }
    updated: list[SweepCandidateRecord] = []
    for record in records:
        if record.status != "completed":
            updated.append(record)
            continue
        baseline = _matching_baseline(stage_id, record, records)
        payload = full_payloads[record.candidate.config_id]
        baseline_payload = full_payloads[baseline.candidate.config_id]
        diagnostics = _relative_guardrail_diagnostics(
            record,
            baseline,
            payload,
            baseline_payload,
            **thresholds,
        )
        if upstream_eligible_components is not None:
            upstream_status = {
                attribute: getattr(record.candidate, attribute) in allowed
                for attribute, allowed in upstream_eligible_components.items()
            }
            diagnostics["upstream_component_eligibility"] = upstream_status
            upstream_failures = [
                f"upstream-{attribute}-guardrail-failed"
                for attribute, passed in upstream_status.items()
                if not passed
            ]
            diagnostics["failures"].extend(upstream_failures)
            diagnostics["passed"] = not diagnostics["failures"]
        payload["guardrail_diagnostics"] = diagnostics
        payload["guardrails_passed"] = bool(diagnostics["passed"])
        payload["guardrail_finalized"] = True
        rewritten = _write_candidate_record(
            Path(record.result_path),
            candidate=record.candidate,
            input_fingerprint=record.input_fingerprint,
            status=record.status,
            payload=payload,
        )
        updated.append(replace(rewritten, resumed=record.resumed))
    return tuple(updated)


def _validate_inputs(
    config: Mapping[str, Any],
    documents: Mapping[str, CanonicalDocument],
    questions: Sequence[Mapping[str, Any]],
    notes: Mapping[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    benchmark = config.get("benchmark")
    if not isinstance(benchmark, Mapping):
        raise SweepContractError("config.benchmark must be a mapping")
    retrieval = config.get("retrieval")
    if (
        not isinstance(retrieval, Mapping)
        or retrieval.get("scope") != "paper-scoped"
    ):
        raise SweepContractError(
            "config.retrieval.scope must be exactly 'paper-scoped'"
        )
    paper_ids = tuple(sorted(documents))
    question_ids = tuple(sorted(str(row.get("row_id")) for row in questions))
    if len(paper_ids) != int(benchmark.get("paper_count", -1)):
        raise SweepContractError(
            f"expected {benchmark.get('paper_count')} documents, "
            f"found {len(paper_ids)}"
        )
    if len(question_ids) != int(benchmark.get("question_count", -1)):
        raise SweepContractError(
            f"expected {benchmark.get('question_count')} questions, "
            f"found {len(question_ids)}"
        )
    if len(question_ids) != len(set(question_ids)) or any(
        not value or value == "None" for value in question_ids
    ):
        raise SweepContractError("question row_ids must be non-empty and unique")
    normalized_documents = {normalize_paper_id(value) for value in paper_ids}
    if normalized_documents != set(paper_ids):
        raise SweepContractError("document keys must use normalized paper IDs")
    missing_notes = sorted(set(paper_ids) - set(notes))
    blank_notes = sorted(
        paper_id
        for paper_id in paper_ids
        if paper_id in notes and not notes[paper_id].strip()
    )
    if missing_notes or blank_notes:
        raise SweepContractError(
            "Gate C requires complete frozen notes before the sweep: "
            f"missing={missing_notes}, blank={blank_notes}"
        )
    return paper_ids, question_ids


def _frozen_confirmation_selection(
    config: Mapping[str, Any],
) -> ConfirmationSelection:
    stages = config.get("stages")
    top2 = (
        stages.get("top2_confirmation")
        if isinstance(stages, Mapping)
        else None
    )
    if not isinstance(top2, Mapping):
        raise SweepContractError(
            "config.stages.top2_confirmation must be a mapping"
        )

    def selected(key: str) -> tuple[str, ...]:
        raw = top2.get(key)
        if (
            not isinstance(raw, list)
            or len(raw) != 2
            or len(set(raw)) != 2
            or any(not isinstance(value, str) or not value for value in raw)
        ):
            raise SweepContractError(
                f"config.stages.top2_confirmation.{key} "
                "must contain two unique IDs"
            )
        return tuple(raw)

    selection = ConfirmationSelection(
        pdf_chunkers=selected("selected_pdf_chunkers"),
        retrievers=selected("selected_retrievers"),
        source_compositions=selected(
            "selected_source_compositions"
        ),
        reranker_modes=selected("selected_reranker_modes"),
    )
    if "rerank-off" not in selection.reranker_modes:
        raise SweepContractError(
            "frozen confirmation must include rerank-off"
        )
    return selection


def _frozen_stage_anchors(
    config: Mapping[str, Any],
) -> Mapping[str, str]:
    stages = config.get("stages")
    anchors = (
        stages.get("stage_anchors")
        if isinstance(stages, Mapping)
        else None
    )
    required = (
        "pdf_chunker",
        "note_chunker",
        "retriever",
        "source_composition",
    )
    if (
        not isinstance(anchors, Mapping)
        or set(anchors) != set(required)
        or any(
            not isinstance(anchors.get(key), str)
            or not anchors[key]
            for key in required
        )
    ):
        raise SweepContractError(
            "config.stages.stage_anchors is incomplete"
        )
    return {key: str(anchors[key]) for key in required}


def _rank_stage(
    stage_id: str,
    records: Sequence[SweepCandidateRecord],
    *,
    expected_paper_ids: Sequence[str],
    expected_question_ids: Sequence[str],
    tie_threshold: float,
) -> SweepStageRanking:
    failed = tuple(
        sorted(
            record.candidate.config_id
            for record in records
            if record.status == "failed"
        )
    )
    incomplete = tuple(
        sorted(
            record.candidate.config_id
            for record in records
            if record.status != "failed"
            and not record.is_complete(
                expected_paper_ids=expected_paper_ids,
                expected_question_ids=expected_question_ids,
            )
        )
    )
    pending_guardrails = tuple(
        sorted(
            record.candidate.config_id
            for record in records
            if record.status == "completed"
            and not record.guardrail_finalized
        )
    )
    blocking_failures = tuple(
        sorted(
            record.candidate.config_id
            for record in records
            if record.status == "failed"
            and record.failure_kind != "strategy"
        )
    )
    if blocking_failures or incomplete or pending_guardrails:
        reasons = []
        if blocking_failures:
            reasons.append(
                "infrastructure/unknown failures: "
                + ", ".join(blocking_failures)
            )
        if incomplete:
            reasons.append(
                "incomplete required candidates: " + ", ".join(incomplete)
            )
        if pending_guardrails:
            reasons.append(
                "guardrail finalization pending: "
                + ", ".join(pending_guardrails)
            )
        return SweepStageRanking(
            stage_id=stage_id,
            ranked=(),
            failed_config_ids=failed,
            incomplete_config_ids=incomplete,
            pending_guardrail_config_ids=pending_guardrails,
            ineligible_config_ids=(),
            evaluable_set_fingerprint=None,
            status="blocked",
            error=f"{stage_id}: {'; '.join(reasons)}",
        )
    eligible: list[SweepCandidateRecord] = []
    ineligible: list[str] = []
    for record in records:
        if record.candidate.config_id in failed + incomplete:
            continue
        if (
            not record.candidate.rankable
            or not record.mapping_passed
            or not record.guardrails_passed
            or record.primary is None
        ):
            ineligible.append(record.candidate.config_id)
            continue
        eligible.append(record)

    evaluable_sets = {record.evaluable_set for record in eligible}
    if len(evaluable_sets) > 1:
        return SweepStageRanking(
            stage_id=stage_id,
            ranked=(),
            failed_config_ids=failed,
            incomplete_config_ids=incomplete,
            pending_guardrail_config_ids=pending_guardrails,
            ineligible_config_ids=tuple(
                sorted(
                    set(ineligible)
                    | {
                        record.candidate.config_id
                        for record in eligible
                    }
                )
            ),
            evaluable_set_fingerprint=None,
            status="blocked",
            error=(
                f"{stage_id}: candidates do not share the same evaluable set"
            ),
        )
    summaries = [
        record.summary(
            expected_paper_ids=expected_paper_ids,
            expected_question_ids=expected_question_ids,
        )
        for record in eligible
    ]
    ordered_summaries = rank_candidates(
        summaries,
        tie_threshold=tie_threshold,
    )
    by_id = {record.candidate.config_id: record for record in eligible}
    evaluable = next(iter(evaluable_sets), None)
    return SweepStageRanking(
        stage_id=stage_id,
        ranked=tuple(
            by_id[summary.config_id] for summary in ordered_summaries
        ),
        failed_config_ids=failed,
        incomplete_config_ids=incomplete,
        pending_guardrail_config_ids=pending_guardrails,
        ineligible_config_ids=tuple(sorted(ineligible)),
        evaluable_set_fingerprint=(
            fingerprint_payload(sorted(evaluable))
            if evaluable is not None
            else None
        ),
    )


def _write_stage_artifacts(
    run_root: Path,
    ranking: SweepStageRanking,
    records: Sequence[SweepCandidateRecord],
    *,
    expected_paper_ids: Sequence[str],
    expected_question_ids: Sequence[str],
) -> tuple[Path, ...]:
    stage_root = run_root / "sweep" / "stages" / ranking.stage_id
    ranking_path = _atomic_write_json(
        stage_root / "ranking.json",
        ranking.to_dict(),
    )
    unmapped_path = _atomic_write_json(
        stage_root / "unmapped.json",
        {
            "schema_version": SWEEP_SCHEMA_VERSION,
            "stage_id": ranking.stage_id,
            "candidates": [
                {
                    "config_id": record.candidate.config_id,
                    "unmapped": (
                        record.payload.get("mapping", {}).get("unmapped", [])
                        if isinstance(record.payload.get("mapping"), Mapping)
                        else []
                    ),
                }
                for record in records
            ],
        },
    )
    completeness_path = _atomic_write_json(
        stage_root / "completeness.json",
        {
            "schema_version": SWEEP_SCHEMA_VERSION,
            "stage_id": ranking.stage_id,
            "expected_paper_count": len(expected_paper_ids),
            "expected_question_count": len(expected_question_ids),
            "candidates": [
                {
                    "config_id": record.candidate.config_id,
                    "status": record.status,
                    "complete": record.is_complete(
                        expected_paper_ids=expected_paper_ids,
                        expected_question_ids=expected_question_ids,
                    ),
                    "completed_paper_count": len(record.completed_paper_ids),
                    "completed_question_count": len(
                        record.completed_question_ids
                    ),
                    "failure_kind": record.failure_kind,
                    "error": record.error,
                }
                for record in records
            ],
        },
    )
    guardrails_path = _atomic_write_json(
        stage_root / "guardrails.json",
        {
            "schema_version": SWEEP_SCHEMA_VERSION,
            "stage_id": ranking.stage_id,
            "candidates": [
                {
                    "config_id": record.candidate.config_id,
                    "status": record.status,
                    "guardrail_finalized": record.guardrail_finalized,
                    "guardrails_passed": record.guardrails_passed,
                    "diagnostics": record.payload.get(
                        "guardrail_diagnostics"
                    ),
                }
                for record in records
            ],
        },
    )
    return (
        ranking_path,
        unmapped_path,
        completeness_path,
        guardrails_path,
    )


def _pareto_frontier(
    records: Sequence[SweepCandidateRecord],
) -> tuple[Mapping[str, object], ...]:
    points = [
        {
            "config_id": record.candidate.config_id,
            "stage_id": record.candidate.stage_id,
            "primary": float(record.primary),
            "p95_latency_ms": record.p95_latency_ms,
            "index_bytes": record.index_bytes,
            "chunk_count": record.chunk_count,
            "status": record.status,
            "guardrails_passed": record.guardrails_passed,
        }
        for record in records
        if record.primary is not None
    ]
    frontier = []
    for point in points:
        dominated = any(
            other["config_id"] != point["config_id"]
            and other["primary"] >= point["primary"]
            and other["p95_latency_ms"] <= point["p95_latency_ms"]
            and other["index_bytes"] <= point["index_bytes"]
            and (
                other["primary"] > point["primary"]
                or other["p95_latency_ms"] < point["p95_latency_ms"]
                or other["index_bytes"] < point["index_bytes"]
            )
            for other in points
        )
        if not dominated:
            frontier.append(point)
    ordered = sorted(
        frontier,
        key=lambda point: (
            -float(point["primary"]),
            float(point["p95_latency_ms"]),
            int(point["index_bytes"]),
            str(point["config_id"]),
        ),
    )
    return tuple(
        {"rank": rank, **point}
        for rank, point in enumerate(ordered, 1)
    )


def _score_summary(
    record: SweepCandidateRecord,
) -> Mapping[str, Any]:
    summary = record.payload.get("score_summary")
    if not isinstance(summary, Mapping):
        raise SweepContractError(
            f"{record.candidate.config_id}: score_summary is missing"
        )
    return summary


def _metric_rows(
    record: SweepCandidateRecord,
    axis: str,
) -> Mapping[str, Mapping[str, object]]:
    values = _score_summary(record).get(axis)
    if not isinstance(values, Mapping):
        raise SweepContractError(
            f"{record.candidate.config_id}: score_summary.{axis} is missing"
        )
    rows: dict[str, Mapping[str, object]] = {}
    for key, metrics in values.items():
        if not isinstance(key, str) or not isinstance(metrics, Mapping):
            raise SweepContractError(
                f"{record.candidate.config_id}: invalid {axis} score row"
            )
        rows[key] = metrics
    return rows


def _paper_domains(
    record: SweepCandidateRecord,
) -> Mapping[str, str]:
    compact = record.payload.get("paper_domains")
    if isinstance(compact, Mapping):
        domains = {
            str(paper_id): str(domain)
            for paper_id, domain in compact.items()
            if str(paper_id) and str(domain)
        }
        if len(domains) != len(compact):
            raise SweepContractError(
                f"{record.candidate.config_id}: blank paper/domain"
            )
        return domains

    domains: dict[str, str] = {}
    rows = record.payload.get("question_results")
    if not isinstance(rows, list):
        raise SweepContractError(
            f"{record.candidate.config_id}: question_results is missing"
        )
    for row in rows:
        if not isinstance(row, Mapping):
            raise SweepContractError(
                f"{record.candidate.config_id}: invalid question result"
            )
        paper_id = str(row.get("paper_id") or "")
        domain = str(row.get("domain") or "")
        if not paper_id or not domain:
            raise SweepContractError(
                f"{record.candidate.config_id}: blank paper/domain"
            )
        previous = domains.setdefault(paper_id, domain)
        if previous != domain:
            raise SweepContractError(
                f"{record.candidate.config_id}: paper {paper_id} spans domains"
            )
    return domains


def _find_c0_baseline(
    records: Sequence[SweepCandidateRecord],
) -> SweepCandidateRecord:
    matches = [
        record
        for record in records
        if record.candidate.stage_id == "pdf-chunker"
        and record.candidate.pdf_chunker == "pdf-fixed-800"
        and record.candidate.retriever == "dense"
        and record.candidate.source_composition == "pdf-only"
        and record.candidate.reranker == "rerank-off"
    ]
    if len(matches) != 1:
        raise SweepContractError(
            f"expected exactly one C0 baseline, found {len(matches)}"
        )
    baseline = matches[0]
    if baseline.status != "completed" or not baseline.mapping_passed:
        raise SweepContractError("C0 baseline is not complete and mapping-passed")
    return baseline


def _finite_paper_metric(
    record: SweepCandidateRecord,
    metric: str,
) -> Mapping[str, float]:
    values: dict[str, float] = {}
    for paper_id, metrics in _metric_rows(record, "by_paper").items():
        value = metrics.get(metric)
        if value is None or not math.isfinite(float(value)):
            raise SweepContractError(
                f"{record.candidate.config_id}: {paper_id} has no finite "
                f"{metric}"
            )
        values[paper_id] = float(value)
    return values


def _metric_names(
    records: Sequence[SweepCandidateRecord],
) -> tuple[str, ...]:
    names: set[str] = set()
    for record in records:
        overall = _score_summary(record).get("overall")
        if isinstance(overall, Mapping):
            names.update(str(name) for name in overall)
    return tuple(sorted(names))


def _write_final_report_artifacts(
    *,
    config: Mapping[str, Any],
    root: Path,
    records: Sequence[SweepCandidateRecord],
    rankings: Mapping[str, SweepStageRanking],
    winner: SweepCandidateRecord,
) -> tuple[tuple[Path, ...], Mapping[str, object]]:
    baseline = _find_c0_baseline(records)
    bootstrap_config = config.get("bootstrap")
    if not isinstance(bootstrap_config, Mapping):
        raise SweepContractError("config.bootstrap must be a mapping")
    bootstrap_metric = "coverage_ndcg_at_10"
    winner_by_paper = _finite_paper_metric(winner, bootstrap_metric)
    baseline_by_paper = _finite_paper_metric(baseline, bootstrap_metric)
    paper_domains = _paper_domains(winner)
    bootstrap = paired_bootstrap(
        winner_by_paper,
        baseline_by_paper,
        paper_domains,
        samples=int(bootstrap_config.get("samples", 0)),
        confidence=float(bootstrap_config.get("confidence", 0.0)),
        seed=str(bootstrap_config.get("seed") or ""),
    )
    bootstrap_payload = {
        "schema_version": SWEEP_SCHEMA_VERSION,
        "metric": bootstrap_metric,
        "candidate_config_id": winner.candidate.config_id,
        "baseline_config_id": baseline.candidate.config_id,
        **bootstrap.to_dict(),
    }

    stage_rank_by_id = {
        record.candidate.config_id: rank
        for ranking in rankings.values()
        for rank, record in enumerate(ranking.ranked, 1)
    }
    leaderboard_rows = []
    for record in records:
        candidate = record.candidate
        leaderboard_rows.append(
            {
                "stage_id": candidate.stage_id,
                "stage_rank": stage_rank_by_id.get(candidate.config_id, ""),
                "config_id": candidate.config_id,
                "status": record.status,
                "rankable": candidate.rankable,
                "mapping_passed": record.mapping_passed,
                "guardrails_passed": record.guardrails_passed,
                "primary_metric": record.payload.get("primary_metric", ""),
                "primary_score": (
                    "" if record.primary is None else record.primary
                ),
                "p95_latency_ms": record.p95_latency_ms,
                "index_bytes": record.index_bytes,
                "chunk_count": record.chunk_count,
                "pdf_chunker": candidate.pdf_chunker,
                "note_chunker": candidate.note_chunker or "",
                "retriever": candidate.retriever,
                "source_composition": candidate.source_composition,
                "reranker": candidate.reranker,
            }
        )
    leaderboard_rows.sort(
        key=lambda row: (
            str(row["stage_id"]),
            (
                int(row["stage_rank"])
                if isinstance(row["stage_rank"], int)
                else 1_000_000
            ),
            str(row["config_id"]),
        )
    )

    metrics = _metric_names((baseline, winner))
    breakdown_rows = []
    for label, record in (("baseline", baseline), ("winner", winner)):
        for axis, scope in (
            ("by_paper", "paper"),
            ("by_domain", "domain"),
            ("by_question_type", "question_type"),
        ):
            for key, values in sorted(_metric_rows(record, axis).items()):
                breakdown_rows.append(
                    {
                        "role": label,
                        "config_id": record.candidate.config_id,
                        "scope": scope,
                        "key": key,
                        "domain": (
                            paper_domains.get(key, "")
                            if scope == "paper"
                            else (key if scope == "domain" else "")
                        ),
                        **{
                            metric: (
                                "" if values.get(metric) is None
                                else values.get(metric)
                            )
                            for metric in metrics
                        },
                    }
                )

    blocked_rows: list[Mapping[str, object]] = []
    ineligible_ids = {
        config_id
        for ranking in rankings.values()
        for config_id in ranking.ineligible_config_ids
    }
    for record in records:
        if (
            record.status != "completed"
            or record.candidate.config_id in ineligible_ids
        ):
            blocked_rows.append(
                {
                    "kind": "candidate",
                    "stage_id": record.candidate.stage_id,
                    "config_id": record.candidate.config_id,
                    "status": record.status,
                    "error": record.error,
                    "rankable": record.candidate.rankable,
                    "mapping_passed": record.mapping_passed,
                    "guardrails_passed": record.guardrails_passed,
                }
            )
        mapping = record.payload.get("mapping")
        unmapped = (
            mapping.get("unmapped")
            if isinstance(mapping, Mapping)
            else None
        )
        if isinstance(unmapped, list):
            blocked_rows.extend(
                {
                    "kind": "unmapped-evidence",
                    "stage_id": record.candidate.stage_id,
                    "config_id": record.candidate.config_id,
                    **dict(item),
                }
                for item in unmapped
                if isinstance(item, Mapping)
            )

    report_root = root / "report"
    leaderboard_csv = _atomic_write_csv(
        report_root / "leaderboard.csv",
        leaderboard_rows,
        fieldnames=tuple(leaderboard_rows[0]),
    )
    breakdown_csv = _atomic_write_csv(
        report_root / "paper-domain-breakdown.csv",
        breakdown_rows,
        fieldnames=tuple(breakdown_rows[0]),
    )
    bootstrap_path = _atomic_write_json(
        report_root / "paired-bootstrap.json",
        bootstrap_payload,
    )
    blocked_path = _atomic_write_jsonl(
        report_root / "blocked-and-unmapped.jsonl",
        blocked_rows,
    )
    winner_overall = _score_summary(winner).get("overall")
    baseline_overall = _score_summary(baseline).get("overall")
    if not isinstance(winner_overall, Mapping) or not isinstance(
        baseline_overall, Mapping
    ):
        raise SweepContractError("winner/baseline overall scores are missing")
    stage_lines = [
        f"| {stage_id} | {ranking.ranked[0].candidate.config_id} | "
        f"{ranking.ranked[0].primary:.6f} |"
        for stage_id, ranking in rankings.items()
    ]
    morning_report = "\n".join(
        (
            "# ResearchQA rq-2 morning report",
            "",
            f"- Retrieval scope: `{config['retrieval']['scope']}`",
            f"- Candidates: {len(records)} total strategy records",
            f"- Completed candidates: "
            f"{sum(record.status == 'completed' for record in records)}",
            f"- Failed candidates: "
            f"{sum(record.status == 'failed' for record in records)}",
            f"- Incomplete candidates: "
            f"{sum(record.status == 'incomplete' for record in records)}",
            f"- Rankable candidates: "
            f"{sum(record.candidate.rankable for record in records)}",
            f"- Provisional winner: `{winner.candidate.config_id}`",
            f"- C0 baseline: `{baseline.candidate.config_id}`",
            f"- Winner {bootstrap_metric}: "
            f"{float(winner_overall[bootstrap_metric]):.6f}",
            f"- Baseline {bootstrap_metric}: "
            f"{float(baseline_overall[bootstrap_metric]):.6f}",
            f"- Paired delta: {bootstrap.observed_delta:+.6f} "
            f"(95% CI {bootstrap.lower:+.6f} to {bootstrap.upper:+.6f}; "
            f"{bootstrap.samples:,} domain-stratified paper resamples)",
            f"- Winner p95 retrieval latency: "
            f"{winner.p95_latency_ms:.3f} ms",
            "",
            "| Stage | Winner | Primary |",
            "|---|---|---:|",
            *stage_lines,
            "",
            "The winner is provisional. This run stops after rq-2 and does "
            "not start rq-5 automatically.",
            "",
        )
    )
    morning_path = _atomic_write_bytes(
        report_root / "morning-report.md",
        morning_report.encode("utf-8"),
    )
    return (
        (
            leaderboard_csv,
            breakdown_csv,
            bootstrap_path,
            blocked_path,
            morning_path,
        ),
        bootstrap_payload,
    )


def run_strategy_sweep(
    config: Mapping[str, Any],
    run_root: str | Path,
    documents: Mapping[str, CanonicalDocument],
    questions: Sequence[Mapping[str, Any]],
    frozen_notes: Mapping[str, str],
    embedder: EmbedderAdapter,
    reranker: object,
    *,
    before_rerank_stage: BeforeRerankCallback,
    assert_embedding_cache_only: CacheOnlyCallback,
    candidate_executor: CandidateExecutor = run_complete_candidate,
    guardrail_check: GuardrailCheck | None = None,
) -> StrategySweepResult:
    """Run the complete recoverable strategy sweep and final confirmation."""

    root = Path(run_root).resolve(strict=False)
    paper_ids, question_ids = _validate_inputs(
        config,
        documents,
        questions,
        frozen_notes,
    )
    decision = config.get("decision")
    gates = config.get("gates")
    if not isinstance(decision, Mapping) or not isinstance(gates, Mapping):
        raise SweepContractError("config decision/gates must be mappings")
    tie_threshold = float(decision.get("primary_tie_threshold", 0.005))
    mapping_overall = float(gates.get("mapping_overall_minimum", 0.95))
    mapping_per_paper = float(
        gates.get("mapping_per_paper_minimum", 0.90)
    )
    performance = config.get("performance")
    if not isinstance(performance, Mapping):
        raise SweepContractError("config.performance must be a mapping")
    performance_sample_count = int(
        performance.get("sample_question_count", 0)
    )
    performance_warmup_passes = int(performance.get("warmup_passes", 0))
    performance_timed_passes = int(performance.get("timed_passes", 0))
    if (
        performance_sample_count <= 0
        or performance_warmup_passes <= 0
        or performance_timed_passes <= 0
    ):
        raise SweepContractError(
            "config.performance counts must be greater than zero"
        )
    metrics = config.get("metrics")
    if not isinstance(metrics, Mapping):
        raise SweepContractError("config.metrics must be a mapping")
    configured_guardrails = metrics.get("guardrails")
    if (
        not isinstance(configured_guardrails, list)
        or not configured_guardrails
        or any(
            not isinstance(metric, str) or not metric
            for metric in configured_guardrails
        )
    ):
        raise SweepContractError(
            "config.metrics.guardrails must be a non-empty string array"
        )
    config_fingerprint = fingerprint_payload(config)
    stage_anchors = _frozen_stage_anchors(config)

    all_records: list[SweepCandidateRecord] = []
    rankings: dict[str, SweepStageRanking] = {}
    artifact_paths: list[Path] = []
    evidence_mapping_cache: dict[str, EvidenceMappingBundle] = {}
    rerank_entered = False

    def execute_stage(
        stage_id: str,
        candidates: Sequence[StrategyCandidate],
        *,
        rerank_phase: bool,
    ) -> SweepStageRanking:
        nonlocal rerank_entered
        stage_records: list[SweepCandidateRecord] = []
        for candidate in candidates:
            input_fingerprint = _candidate_input_fingerprint(
                candidate,
                config_fingerprint=config_fingerprint,
                documents=documents,
                questions=questions,
                notes=frozen_notes,
                embedder=embedder,
                reranker=reranker,
            )
            path = _result_path(root, candidate)
            record = _load_candidate_record(
                path,
                candidate=candidate,
                input_fingerprint=input_fingerprint,
            )
            if record is None:
                if rerank_phase:
                    if not rerank_entered:
                        before_rerank_stage()
                        rerank_entered = True
                    assert_embedding_cache_only(candidate)
                try:
                    result = candidate_executor(
                        candidate,
                        documents,
                        questions,
                        expected_paper_ids=paper_ids,
                        expected_question_ids=question_ids,
                        embedder=embedder,
                        reranker=reranker,
                        notes=frozen_notes,
                        mapping_overall_minimum=mapping_overall,
                        mapping_per_paper_minimum=mapping_per_paper,
                        performance_sample_question_count=(
                            performance_sample_count
                        ),
                        performance_warmup_passes=(
                            performance_warmup_passes
                        ),
                        performance_timed_passes=(
                            performance_timed_passes
                        ),
                        evidence_mapping_cache=evidence_mapping_cache,
                    )
                    metric_bundle_complete = all(
                        (
                            value := result.aggregate.overall.get(metric)
                        )
                        is not None
                        and math.isfinite(float(value))
                        for metric in configured_guardrails
                    )
                    custom_guardrail_passed = (
                        True
                        if guardrail_check is None
                        else bool(guardrail_check(result))
                    )
                    result = replace(
                        result,
                        guardrails_passed=(
                            result.guardrails_passed
                            and metric_bundle_complete
                            and custom_guardrail_passed
                        ),
                    )
                    full_payload = result.to_dict()
                    full_payload["metric_bundle_complete"] = (
                        metric_bundle_complete
                    )
                    full_payload["custom_guardrail_passed"] = (
                        custom_guardrail_passed
                    )
                    complete = result.is_complete(
                        expected_paper_ids=paper_ids,
                        expected_question_ids=question_ids,
                    )
                    full_payload["execution_complete"] = complete
                    full_payload["guardrail_finalized"] = False
                    record = _write_candidate_record(
                        path,
                        candidate=candidate,
                        input_fingerprint=input_fingerprint,
                        status="completed" if complete else "incomplete",
                        payload=full_payload,
                    )
                    del full_payload
                    del result
                except ModelTransportError:
                    # The outer overnight runner owns the bounded 5/20/60
                    # retry policy.  Persisting a failed candidate here would
                    # incorrectly turn a transient Ollama outage into a
                    # deterministic, permanently resumed checkpoint.
                    raise
                except Exception as exc:
                    record = _write_candidate_record(
                        path,
                        candidate=candidate,
                        input_fingerprint=input_fingerprint,
                        status="failed",
                        payload={
                            "candidate": candidate.to_dict(),
                            "execution_complete": False,
                            "failure_kind": _failure_kind(exc),
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "failure_context": {
                                "phase": "candidate-execution",
                                "row_id": None,
                                "pass_index": None,
                                "progress": {
                                    "completed_paper_ids": [],
                                    "completed_question_ids": [],
                                },
                            },
                            "traceback": traceback.format_exc(),
                            "guardrail_finalized": False,
                        },
                    )
            gc.collect()
            stage_records.append(record)
            artifact_paths.append(Path(record.result_path))

        upstream_eligible_components = None
        if stage_id == "top2-confirmation":
            upstream_eligible_components = {
                "pdf_chunker": frozenset(
                    record.candidate.pdf_chunker
                    for record in rankings["pdf-chunker"].ranked
                ),
                "retriever": frozenset(
                    record.candidate.retriever
                    for record in rankings["retriever"].ranked
                ),
                "source_composition": frozenset(
                    record.candidate.source_composition
                    for record in rankings["source-composition"].ranked
                ),
            }
        stage_records = list(
            _apply_relative_guardrails(
                stage_id,
                stage_records,
                gates,
                upstream_eligible_components=(
                    upstream_eligible_components
                ),
            )
        )
        all_records.extend(stage_records)
        ranking = _rank_stage(
            stage_id,
            stage_records,
            expected_paper_ids=paper_ids,
            expected_question_ids=question_ids,
            tie_threshold=tie_threshold,
        )
        paths = _write_stage_artifacts(
            root,
            ranking,
            stage_records,
            expected_paper_ids=paper_ids,
            expected_question_ids=question_ids,
        )
        artifact_paths.extend(paths)
        rankings[stage_id] = ranking
        if ranking.error is not None:
            raise SweepContractError(ranking.error)
        if not ranking.ranked:
            raise SweepContractError(
                f"{stage_id}: no complete eligible candidate"
            )
        return ranking

    initial = generate_orthogonal_candidates(config)
    pdf_ranking = execute_stage(
        "pdf-chunker",
        initial.stages["pdf-chunker"],
        rerank_phase=False,
    )
    best_pdf = stage_anchors["pdf_chunker"]

    note_plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker=best_pdf,
    )
    note_ranking = execute_stage(
        "note-chunker",
        note_plan.stages["note-chunker"],
        rerank_phase=False,
    )
    best_note = stage_anchors["note_chunker"]

    retrieval_plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker=best_pdf,
        anchor_note_chunker=best_note,
    )
    retrieval_ranking = execute_stage(
        "retriever",
        retrieval_plan.stages["retriever"],
        rerank_phase=False,
    )
    best_retriever = stage_anchors["retriever"]

    composition_plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker=best_pdf,
        anchor_note_chunker=best_note,
        anchor_retriever=best_retriever,
    )
    composition_ranking = execute_stage(
        "source-composition",
        composition_plan.stages["source-composition"],
        rerank_phase=False,
    )
    best_composition = stage_anchors["source_composition"]

    reranker_plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker=best_pdf,
        anchor_note_chunker=best_note,
        anchor_retriever=best_retriever,
        anchor_source_composition=best_composition,
    )
    reranker_ranking = execute_stage(
        "reranker",
        reranker_plan.stages["reranker"],
        rerank_phase=True,
    )
    confirmation = _frozen_confirmation_selection(config)
    eligible_component_values = {
        "pdf_chunkers": {
            record.candidate.pdf_chunker for record in pdf_ranking.ranked
        },
        "retrievers": {
            record.candidate.retriever
            for record in retrieval_ranking.ranked
        },
        "source_compositions": {
            record.candidate.source_composition
            for record in composition_ranking.ranked
        },
        "reranker_modes": {
            record.candidate.reranker for record in reranker_ranking.ranked
        },
    }
    confirmation_diagnostic_fallbacks = {
        field: [
            value
            for value in getattr(confirmation, field)
            if value not in eligible_component_values[field]
        ]
        for field in (
            "pdf_chunkers",
            "retrievers",
            "source_compositions",
            "reranker_modes",
        )
    }
    confirmation_plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker=best_pdf,
        anchor_note_chunker=best_note,
        anchor_retriever=best_retriever,
        anchor_source_composition=best_composition,
        best_reranker=confirmation.reranker_modes[1],
        confirmation=confirmation,
    )
    confirmation_ranking = execute_stage(
        "top2-confirmation",
        confirmation_plan.stages["top2-confirmation"],
        rerank_phase=True,
    )

    if len({record.candidate.config_id for record in all_records}) != len(
        all_records
    ):
        raise SweepContractError("sweep generated duplicate candidate IDs")
    winner = confirmation_ranking.ranked[0]
    leaderboard = tuple(
        {
            "rank": rank,
            "config_id": record.candidate.config_id,
            "stage_id": record.candidate.stage_id,
            "primary": record.primary,
            "p95_latency_ms": record.p95_latency_ms,
            "index_bytes": record.index_bytes,
            "chunk_count": record.chunk_count,
            "status": record.status,
            "guardrails_passed": record.guardrails_passed,
            "candidate": record.candidate.to_dict(),
        }
        for rank, record in enumerate(confirmation_ranking.ranked, 1)
    )
    frontier = _pareto_frontier(confirmation_ranking.ranked)
    report_paths, bootstrap_payload = _write_final_report_artifacts(
        config=config,
        root=root,
        records=all_records,
        rankings=rankings,
        winner=winner,
    )
    final_root = root / "sweep" / "final"
    leaderboard_path = _atomic_write_json(
        final_root / "leaderboard.json",
        {
            "schema_version": SWEEP_SCHEMA_VERSION,
            "rows": list(leaderboard),
        },
    )
    pareto_path = _atomic_write_json(
        final_root / "pareto-frontier.json",
        {
            "schema_version": SWEEP_SCHEMA_VERSION,
            "rows": list(frontier),
        },
    )
    decision_path = _atomic_write_json(
        final_root / "decision-summary.json",
        {
            "schema_version": SWEEP_SCHEMA_VERSION,
            "winner_label": "provisional",
            "provisional_winner": winner.candidate.config_id,
            "winner_candidate": winner.candidate.to_dict(),
            "primary": winner.primary,
            "guardrails_passed": winner.guardrails_passed,
            "bootstrap": bootstrap_payload,
            "tie_threshold": tie_threshold,
            "confirmation_count": len(
                [
                    record
                    for record in all_records
                    if record.candidate.stage_id == "top2-confirmation"
                ]
            ),
            "confirmation_diagnostic_fallbacks": (
                confirmation_diagnostic_fallbacks
            ),
            "stage_anchors": dict(stage_anchors),
            "stage_top2": {
                stage_id: [
                    record.candidate.config_id for record in ranking.top2
                ]
                for stage_id, ranking in rankings.items()
            },
            "stop_after_report": True,
        },
    )
    artifact_paths.extend(
        (
            leaderboard_path,
            pareto_path,
            decision_path,
            *report_paths,
        )
    )
    return StrategySweepResult(
        records=tuple(all_records),
        stage_rankings=dict(rankings),
        provisional_winner=winner.candidate.config_id,
        leaderboard=leaderboard,
        pareto_frontier=frontier,
        artifact_paths=tuple(
            str(path.resolve()) for path in artifact_paths
        ),
    )


__all__ = [
    "StrategySweepResult",
    "SweepCandidateRecord",
    "SweepContractError",
    "SweepStageRanking",
    "run_strategy_sweep",
]
