"""Recoverable Gate C-F strategy sweep for ResearchQA rq-2.

Each candidate is an independently hash-verified JSON checkpoint.  Stage
rankings are rebuilt from those checkpoints, so a process interruption never
requires recomputing completed candidates and failed/incomplete candidates can
never enter a leaderboard.
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from benchmarks.overnight import canonical_json_bytes, fingerprint_payload
from benchmarks.researchqa_scoring import CandidateSummary, rank_candidates
from benchmarks.researchqa_strategy import (
    CandidateRunResult,
    ConfirmationSelection,
    EmbedderAdapter,
    StrategyCandidate,
    generate_orthogonal_candidates,
    normalize_paper_id,
    run_complete_candidate,
)
from service.pdf_ir import CanonicalDocument


SWEEP_SCHEMA_VERSION = 1
SWEEP_ENGINE_REVISION = "researchqa-sweep-v2"


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
        return bool(self.payload.get("guardrails_passed"))

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
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(canonical_json_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def _result_path(run_root: Path, candidate: StrategyCandidate) -> Path:
    return (
        run_root
        / "sweep"
        / "candidates"
        / candidate.stage_id
        / f"{candidate.config_id}.json"
    )


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
                for key in ("model_id", "revision")
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
        payload=dict(payload),
        result_path=str(path.resolve()),
    )


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
        payload=dict(payload),
        result_path=str(path.resolve()),
        resumed=True,
    )


def _validate_inputs(
    config: Mapping[str, Any],
    documents: Mapping[str, CanonicalDocument],
    questions: Sequence[Mapping[str, Any]],
    notes: Mapping[str, str],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    benchmark = config.get("benchmark")
    if not isinstance(benchmark, Mapping):
        raise SweepContractError("config.benchmark must be a mapping")
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
) -> tuple[Path, Path, Path]:
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
                    "error": record.error,
                }
                for record in records
            ],
        },
    )
    return ranking_path, unmapped_path, completeness_path


def _top_components(
    ranking: SweepStageRanking,
    attribute: str,
) -> tuple[str, ...]:
    values: list[str] = []
    for record in ranking.ranked:
        value = str(getattr(record.candidate, attribute))
        if value not in values:
            values.append(value)
        if len(values) == 2:
            break
    if not values:
        raise SweepContractError(
            f"{ranking.stage_id}: no complete eligible candidates"
        )
    return tuple(values)


def _pareto_frontier(
    records: Sequence[SweepCandidateRecord],
) -> tuple[Mapping[str, object], ...]:
    points = [
        {
            "config_id": record.candidate.config_id,
            "primary": float(record.primary),
            "p95_latency_ms": record.p95_latency_ms,
            "index_bytes": record.index_bytes,
            "chunk_count": record.chunk_count,
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
    return tuple(
        sorted(
            frontier,
            key=lambda point: (
                -float(point["primary"]),
                float(point["p95_latency_ms"]),
                int(point["index_bytes"]),
                str(point["config_id"]),
            ),
        )
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
    config_fingerprint = fingerprint_payload(config)

    all_records: list[SweepCandidateRecord] = []
    rankings: dict[str, SweepStageRanking] = {}
    artifact_paths: list[Path] = []
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
                    )
                    if guardrail_check is not None:
                        result = replace(
                            result,
                            guardrails_passed=bool(guardrail_check(result)),
                        )
                    payload = result.to_dict()
                    complete = result.is_complete(
                        expected_paper_ids=paper_ids,
                        expected_question_ids=question_ids,
                    )
                    record = _write_candidate_record(
                        path,
                        candidate=candidate,
                        input_fingerprint=input_fingerprint,
                        status="completed" if complete else "incomplete",
                        payload=payload,
                    )
                except Exception as exc:
                    record = _write_candidate_record(
                        path,
                        candidate=candidate,
                        input_fingerprint=input_fingerprint,
                        status="failed",
                        payload={
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                        },
                    )
            stage_records.append(record)
            all_records.append(record)
            artifact_paths.append(Path(record.result_path))

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
    best_pdf = _top_components(pdf_ranking, "pdf_chunker")[0]

    note_plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker=best_pdf,
    )
    note_ranking = execute_stage(
        "note-chunker",
        note_plan.stages["note-chunker"],
        rerank_phase=False,
    )
    best_note = _top_components(note_ranking, "note_chunker")[0]

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
    best_retriever = _top_components(retrieval_ranking, "retriever")[0]

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
    best_composition = _top_components(
        composition_ranking,
        "source_composition",
    )[0]

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
    best_enabled_reranker = next(
        (
            record.candidate.reranker
            for record in reranker_ranking.ranked
            if record.candidate.reranker != "rerank-off"
        ),
        None,
    )
    if best_enabled_reranker is None:
        raise SweepContractError(
            "reranker stage has no complete enabled reranker"
        )

    confirmation = ConfirmationSelection(
        pdf_chunkers=_top_components(pdf_ranking, "pdf_chunker"),
        retrievers=_top_components(retrieval_ranking, "retriever"),
        source_compositions=_top_components(
            composition_ranking,
            "source_composition",
        ),
        reranker_modes=("rerank-off", best_enabled_reranker),
    )
    confirmation_plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker=best_pdf,
        anchor_note_chunker=best_note,
        anchor_retriever=best_retriever,
        anchor_source_composition=best_composition,
        best_reranker=best_enabled_reranker,
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
            "tie_threshold": tie_threshold,
            "confirmation_count": len(
                [
                    record
                    for record in all_records
                    if record.candidate.stage_id == "top2-confirmation"
                ]
            ),
            "stage_top2": {
                stage_id: [
                    record.candidate.config_id for record in ranking.top2
                ]
                for stage_id, ranking in rankings.items()
            },
            "stop_after_report": True,
        },
    )
    artifact_paths.extend((leaderboard_path, pareto_path, decision_path))
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
