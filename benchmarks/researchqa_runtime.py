"""Fail-closed live runtime assembly for the ResearchQA rq-2 sweep."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from benchmarks.researchqa_chunking import (
    PDF_STRUCTURE_FALLBACK_ID,
    chunk_pdf,
    structure_fallback_corpus_diagnostics,
)
from benchmarks.researchqa_models import (
    OLLAMA_EMBED_DIMENSIONS,
    OLLAMA_EMBED_MODEL_DIGEST,
    OLLAMA_EMBED_MODEL_ID,
    OllamaBatchEmbeddingClient,
    Qwen3RerankerTransformersAdapter,
)
from benchmarks.researchqa_notes import GENERIC_TEMPLATE
from benchmarks.researchqa_retrieval import (
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
)
from benchmarks.researchqa_strategy import (
    R1_RETRIEVER_FUSION_POLICY,
    RR1_RERANK_FUSION_POLICY,
    S1_SOURCE_FUSION_POLICY,
    StrategyCandidate,
    audit_n1_note_route,
    generate_f2_candidate,
    generate_n1_candidate,
    generate_orthogonal_candidates,
    generate_r1_candidate,
    generate_rr1_candidate,
    generate_s1_candidate,
    load_main_documents,
    normalize_paper_id,
)
from benchmarks.researchqa_sweep import (
    StrategySweepResult,
    SweepCandidateRecord,
    run_extension_candidate,
    run_strategy_sweep,
)


RUNTIME_SCHEMA_VERSION = 1
EXPECTED_PAPER_COUNT = 20
EXPECTED_QUESTION_COUNT = 254
EXPECTED_EVALUABLE_QUESTION_COUNT = 239
EXPECTED_MAPPED_GROUP_COUNT = 380
_PAPER_ID_RE = re.compile(r"^W\d+$")

EmbeddingFactory = Callable[..., object]
RerankerFactory = Callable[..., object]
SweepRunner = Callable[..., StrategySweepResult]
ExtensionRunner = Callable[..., SweepCandidateRecord]


class ResearchQARuntimeError(RuntimeError):
    """Raised when the live runtime cannot preserve its input/lifecycle contract."""


@dataclass(frozen=True)
class ResearchQARuntimeResult:
    """Completed sweep plus the two runtime-owned audit artifacts."""

    sweep_result: StrategySweepResult
    model_preflight_path: str
    runtime_summary_path: str


@dataclass(frozen=True)
class ResearchQAExtensionRuntimeResult:
    """Completed extension plus its runtime-owned audit artifacts."""

    extension_id: str
    record: SweepCandidateRecord
    model_preflight_path: str
    prequality_path: str
    runtime_summary_path: str


@dataclass(frozen=True)
class ResearchQANotePrequalityResult:
    """Offline N0/N3/N1 audit with no model lifecycle."""

    candidate_config_id: str
    prequality_path: str
    diagnostics: Mapping[str, object]


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def _read_jsonl(path: Path, *, label: str) -> list[Mapping[str, Any]]:
    if not path.is_file():
        raise ResearchQARuntimeError(f"{label} does not exist: {path}")
    rows: list[Mapping[str, Any]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not raw_line.strip():
            raise ResearchQARuntimeError(
                f"{label} contains a blank line at {line_number}: {path}"
            )
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ResearchQARuntimeError(
                f"{label} line {line_number} is invalid JSON: {exc}"
            ) from exc
        if not isinstance(row, Mapping):
            raise ResearchQARuntimeError(
                f"{label} line {line_number} must be a JSON object"
            )
        rows.append(row)
    return rows


def _strict_paper_id(value: object, *, label: str) -> str:
    try:
        paper_id = normalize_paper_id(value)
    except Exception as exc:
        raise ResearchQARuntimeError(f"{label} has an invalid paper_id") from exc
    if not _PAPER_ID_RE.fullmatch(paper_id):
        raise ResearchQARuntimeError(
            f"{label} paper_id must be a normalized OpenAlex work ID: {paper_id!r}"
        )
    return paper_id


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def load_suite_questions(
    config: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], tuple[str, ...], Path]:
    """Load the immutable cache-owned rq-2 question set."""

    benchmark = config.get("benchmark")
    paths = config.get("paths")
    if not isinstance(benchmark, Mapping) or not isinstance(paths, Mapping):
        raise ResearchQARuntimeError("config benchmark/paths must be mappings")
    if (
        benchmark.get("tier_id") != "rq-2"
        or benchmark.get("paper_count") != EXPECTED_PAPER_COUNT
        or benchmark.get("question_count") != EXPECTED_QUESTION_COUNT
    ):
        raise ResearchQARuntimeError(
            "runtime requires rq-2 with exactly 20 papers and 254 questions"
        )
    cache_root = paths.get("cache_root")
    suite_dir = paths.get("suite_dir")
    if not isinstance(cache_root, str) or not cache_root:
        raise ResearchQARuntimeError("config paths.cache_root must be non-empty")
    if not isinstance(suite_dir, str) or not suite_dir:
        raise ResearchQARuntimeError("config paths.suite_dir must be non-empty")
    question_path = (
        Path(cache_root).resolve(strict=False) / suite_dir / "questions.jsonl"
    ).resolve(strict=False)
    questions = _read_jsonl(question_path, label="suite questions")
    if len(questions) != EXPECTED_QUESTION_COUNT:
        raise ResearchQARuntimeError(
            f"suite questions must contain exactly {EXPECTED_QUESTION_COUNT} "
            f"rows, found {len(questions)}"
        )

    row_ids: set[str] = set()
    paper_ids: set[str] = set()
    for index, question in enumerate(questions, 1):
        row_id = question.get("row_id")
        if not isinstance(row_id, str) or not row_id.strip():
            raise ResearchQARuntimeError(
                f"suite question {index} has an empty row_id"
            )
        if row_id in row_ids:
            raise ResearchQARuntimeError(
                f"suite questions contain duplicate row_id {row_id!r}"
            )
        row_ids.add(row_id)
        paper_ids.add(
            _strict_paper_id(
                question.get("paper_id"),
                label=f"suite question {row_id!r}",
            )
        )
    if len(paper_ids) != EXPECTED_PAPER_COUNT:
        raise ResearchQARuntimeError(
            f"suite questions must cover exactly {EXPECTED_PAPER_COUNT} papers, "
            f"found {len(paper_ids)}"
        )
    return questions, tuple(sorted(paper_ids)), question_path


def load_frozen_notes(
    run_root: str | Path,
    *,
    expected_paper_ids: Sequence[str],
) -> tuple[dict[str, str], Path]:
    """Load and SHA-bind the exact frozen 20-note runtime input."""

    root = Path(run_root).resolve(strict=False)
    frozen_root = root / "note-runs" / "frozen"
    manifest_path = frozen_root / "frozen-notes.jsonl"
    rows = _read_jsonl(manifest_path, label="frozen note manifest")
    if len(rows) != EXPECTED_PAPER_COUNT:
        raise ResearchQARuntimeError(
            f"frozen note manifest must contain exactly {EXPECTED_PAPER_COUNT} "
            f"rows, found {len(rows)}"
        )

    expected = {
        _strict_paper_id(value, label="expected paper set")
        for value in expected_paper_ids
    }
    if len(expected) != EXPECTED_PAPER_COUNT:
        raise ResearchQARuntimeError(
            f"expected paper set must contain exactly {EXPECTED_PAPER_COUNT} "
            f"papers, found {len(expected)}"
        )

    notes: dict[str, str] = {}
    for index, row in enumerate(rows, 1):
        paper_id = _strict_paper_id(
            row.get("paper_id"),
            label=f"frozen note row {index}",
        )
        if row.get("paper_id") != paper_id:
            raise ResearchQARuntimeError(
                f"frozen note row {index} paper_id must already be normalized"
            )
        if paper_id in notes:
            raise ResearchQARuntimeError(
                f"frozen note manifest contains duplicate paper_id {paper_id}"
            )
        if row.get("schema_version") != 1:
            raise ResearchQARuntimeError(
                f"frozen note {paper_id} has unsupported schema_version"
            )
        if row.get("template") != GENERIC_TEMPLATE:
            raise ResearchQARuntimeError(
                f"frozen note {paper_id} must use template {GENERIC_TEMPLATE}"
            )
        expected_sha = row.get("note_sha256")
        if not _is_sha256(expected_sha):
            raise ResearchQARuntimeError(
                f"frozen note {paper_id} has an invalid note_sha256"
            )

        note_path = frozen_root / "notes" / f"{paper_id}.md"
        if not note_path.is_file():
            raise ResearchQARuntimeError(
                f"frozen note file does not exist for {paper_id}: {note_path}"
            )
        note_bytes = note_path.read_bytes()
        actual_sha = _sha256_bytes(note_bytes)
        if actual_sha != expected_sha:
            raise ResearchQARuntimeError(
                f"frozen note SHA-256 mismatch for {paper_id}: "
                f"expected {expected_sha}, found {actual_sha}"
            )
        try:
            note = note_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResearchQARuntimeError(
                f"frozen note {paper_id} is not valid UTF-8"
            ) from exc
        if not note.strip():
            raise ResearchQARuntimeError(f"frozen note {paper_id} is empty")
        notes[paper_id] = note

    if set(notes) != expected:
        raise ResearchQARuntimeError(
            "frozen note paper set differs from suite questions: "
            f"missing={sorted(expected - set(notes))}, "
            f"unexpected={sorted(set(notes) - expected)}"
        )
    return dict(sorted(notes.items())), manifest_path


def _validate_model_config(config: Mapping[str, Any]) -> None:
    retrieval = config.get("retrieval")
    if (
        not isinstance(retrieval, Mapping)
        or retrieval.get("scope") != "paper-scoped"
    ):
        raise ResearchQARuntimeError(
            "config retrieval.scope must be exactly paper-scoped"
        )
    models = config.get("models")
    if not isinstance(models, Mapping):
        raise ResearchQARuntimeError("config models must be a mapping")
    embedding = models.get("embedding")
    reranker = models.get("reranker")
    if not isinstance(embedding, Mapping) or not isinstance(reranker, Mapping):
        raise ResearchQARuntimeError(
            "config models.embedding/models.reranker must be mappings"
        )
    expected_embedding = {
        "model": OLLAMA_EMBED_MODEL_ID,
        "digest": OLLAMA_EMBED_MODEL_DIGEST,
        "dimensions": OLLAMA_EMBED_DIMENSIONS,
    }
    mismatched_embedding = {
        key: embedding.get(key)
        for key, expected in expected_embedding.items()
        if embedding.get(key) != expected
    }
    if mismatched_embedding:
        raise ResearchQARuntimeError(
            f"embedding model config is not pinned: {mismatched_embedding}"
        )
    if (
        reranker.get("model") != RERANKER_MODEL_ID
        or reranker.get("revision") != RERANKER_REVISION
    ):
        raise ResearchQARuntimeError("reranker model config is not pinned")


def _preflight_payload(value: object) -> Mapping[str, Any]:
    to_dict = getattr(value, "to_dict", None)
    payload = to_dict() if callable(to_dict) else value
    if not isinstance(payload, Mapping):
        raise ResearchQARuntimeError(
            "model preflight must return a mapping or expose to_dict()"
        )
    return dict(payload)


def _sweep_summary(result: StrategySweepResult) -> dict[str, Any]:
    return {
        "candidate_count": len(result.records),
        "stage_ids": sorted(result.stage_rankings),
        "provisional_winner": result.provisional_winner,
        "leaderboard_count": len(result.leaderboard),
        "pareto_frontier_count": len(result.pareto_frontier),
        "artifact_paths": list(result.artifact_paths),
    }


def _f2_candidates(
    config: Mapping[str, Any],
) -> tuple[StrategyCandidate, StrategyCandidate]:
    candidate = generate_f2_candidate(config)
    plan = generate_orthogonal_candidates(config)
    baselines = tuple(
        row
        for row in plan.stages.get("pdf-chunker", ())
        if (
            row.pdf_chunker == "pdf-fixed-1200"
            and row.retriever == "dense"
            and row.source_composition == "pdf-only"
            and row.reranker == "rerank-off"
        )
    )
    if len(baselines) != 1:
        raise ResearchQARuntimeError(
            "F2 requires exactly one frozen pdf-fixed-1200 dense baseline"
        )
    return candidate, baselines[0]


def _rr1_candidates(
    config: Mapping[str, Any],
) -> tuple[StrategyCandidate, StrategyCandidate]:
    candidate = generate_rr1_candidate(config)
    plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker="pdf-fixed-1200",
        anchor_note_chunker="note-reviewer-concern",
        anchor_retriever="hybrid-rrf",
        anchor_source_composition="pdf-only",
    )
    baselines = tuple(
        row
        for row in plan.stages.get("reranker", ())
        if (
            row.pdf_chunker == "pdf-fixed-1200"
            and row.retriever == "hybrid-rrf"
            and row.source_composition == "pdf-only"
            and row.reranker == "rerank-off"
        )
    )
    if len(baselines) != 1:
        raise ResearchQARuntimeError(
            "RR1 requires exactly one frozen hybrid rerank-off baseline"
        )
    return candidate, baselines[0]


def _r1_candidates(
    config: Mapping[str, Any],
) -> tuple[StrategyCandidate, StrategyCandidate]:
    candidate = generate_r1_candidate(config)
    plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker="pdf-fixed-1200",
        anchor_note_chunker="note-reviewer-concern",
        anchor_retriever="dense",
        anchor_source_composition="pdf-only",
    )
    baselines = tuple(
        row
        for row in plan.stages.get("retriever", ())
        if (
            row.pdf_chunker == "pdf-fixed-1200"
            and row.retriever == "dense"
            and row.source_composition == "pdf-only"
            and row.reranker == "rerank-off"
        )
    )
    if len(baselines) != 1:
        raise ResearchQARuntimeError(
            "R1 requires exactly one frozen dense rerank-off baseline"
        )
    return candidate, baselines[0]


def _s1_candidates(
    config: Mapping[str, Any],
) -> tuple[StrategyCandidate, StrategyCandidate]:
    candidate = generate_s1_candidate(config)
    plan = generate_orthogonal_candidates(
        config,
        anchor_pdf_chunker="pdf-fixed-1200",
        anchor_note_chunker="note-reviewer-concern",
        anchor_retriever="hybrid-rrf",
        anchor_source_composition="pdf-only",
    )
    baselines = tuple(
        row
        for row in plan.stages.get("source-composition", ())
        if (
            row.pdf_chunker == "pdf-fixed-1200"
            and row.note_chunker is None
            and row.retriever == "hybrid-rrf"
            and row.source_composition == "pdf-only"
            and row.reranker == "rerank-off"
        )
    )
    if len(baselines) != 1:
        raise ResearchQARuntimeError(
            "S1 requires exactly one frozen hybrid PDF-only baseline"
        )
    return candidate, baselines[0]


def _s1_note_prequality(
    candidate: StrategyCandidate,
    documents: Mapping[str, object],
    frozen_notes: Mapping[str, str],
) -> Mapping[str, object]:
    diagnostics = audit_n1_note_route(
        candidate,
        documents,
        frozen_notes,
    )
    eligible_ids = diagnostics.get("eligible_paper_ids")
    fallback_ids = diagnostics.get("fallback_paper_ids")
    base_count = int(diagnostics.get("base_chunk_count", 0))
    backlinkable_base_count = int(
        diagnostics.get("backlinkable_base_chunk_count", 0)
    )
    reviewer_count = int(
        diagnostics.get("reviewer_chunk_count", 0)
    )
    backlinkable_reviewer_count = int(
        diagnostics.get("backlinkable_reviewer_chunk_count", 0)
    )
    if (
        diagnostics.get("contract_status") != "passed"
        or not isinstance(eligible_ids, list)
        or len(eligible_ids) != EXPECTED_PAPER_COUNT
        or fallback_ids != []
        or base_count < EXPECTED_PAPER_COUNT
        or backlinkable_base_count != base_count
        or backlinkable_reviewer_count != reviewer_count
    ):
        raise ResearchQARuntimeError(
            "S1 N0/N3 pre-quality contract failed: "
            f"eligible={eligible_ids}, fallback={fallback_ids}, "
            f"base={backlinkable_base_count}/{base_count}, "
            f"reviewer={backlinkable_reviewer_count}/{reviewer_count}"
        )
    return diagnostics


def _f2_prequality(
    documents: Mapping[str, object],
) -> Mapping[str, object]:
    results = {
        paper_id: chunk_pdf(
            document,
            PDF_STRUCTURE_FALLBACK_ID,
            is_main=True,
        )
        for paper_id, document in sorted(documents.items())
    }
    diagnostics = structure_fallback_corpus_diagnostics(results)
    if diagnostics.get("contract_status") != "passed":
        raise ResearchQARuntimeError(
            "F2 pre-quality contract failed: output/fixed1200="
            f"{float(diagnostics['output_to_fixed_1200_ratio']):.6f}"
        )
    return diagnostics


def run_n0_n3_prequality_runtime(
    config: Mapping[str, Any],
    run_root: str | Path,
) -> ResearchQANotePrequalityResult:
    """Audit the frozen N1 route before any embedding or quality scoring."""

    root = Path(run_root).resolve(strict=False)
    output_path = (
        root
        / "sweep"
        / "extensions"
        / "N0-N3"
        / "runtime"
        / "prequality.json"
    )
    candidate: StrategyCandidate | None = None
    try:
        questions, paper_ids, question_path = load_suite_questions(config)
        frozen_notes, manifest_path = load_frozen_notes(
            root,
            expected_paper_ids=paper_ids,
        )
        documents = load_main_documents(
            root,
            expected_paper_ids=paper_ids,
        )
        if len(documents) != EXPECTED_PAPER_COUNT:
            raise ResearchQARuntimeError(
                f"runtime requires exactly {EXPECTED_PAPER_COUNT} Main documents"
            )
        candidate = generate_n1_candidate(config)
        diagnostics = audit_n1_note_route(
            candidate,
            documents,
            frozen_notes,
        )
        eligible_ids = diagnostics.get("eligible_paper_ids")
        fallback_ids = diagnostics.get("fallback_paper_ids")
        base_count = int(diagnostics.get("base_chunk_count", 0))
        backlinkable_base_count = int(
            diagnostics.get("backlinkable_base_chunk_count", 0)
        )
        reviewer_count = int(
            diagnostics.get("reviewer_chunk_count", 0)
        )
        backlinkable_reviewer_count = int(
            diagnostics.get(
                "backlinkable_reviewer_chunk_count",
                0,
            )
        )
        if (
            diagnostics.get("contract_status") != "passed"
            or not isinstance(eligible_ids, list)
            or len(eligible_ids) != EXPECTED_PAPER_COUNT
            or fallback_ids != []
            or base_count < EXPECTED_PAPER_COUNT
            or backlinkable_base_count != base_count
            or backlinkable_reviewer_count != reviewer_count
        ):
            raise ResearchQARuntimeError(
                "N0/N3 frozen pre-quality contract failed: "
                f"eligible={eligible_ids}, fallback={fallback_ids}, "
                f"base={backlinkable_base_count}/{base_count}, "
                f"reviewer={backlinkable_reviewer_count}/{reviewer_count}"
            )
        payload = {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "extension_id": "N0-N3",
            "status": "completed",
            "candidate": candidate.to_dict(),
            "inputs": {
                "questions_path": str(question_path),
                "frozen_notes_manifest_path": str(manifest_path),
                "paper_count": len(documents),
                "question_count": len(questions),
                "paper_ids": list(paper_ids),
            },
            "diagnostics": diagnostics,
        }
        _atomic_write_json(output_path, payload)
        return ResearchQANotePrequalityResult(
            candidate_config_id=candidate.config_id,
            prequality_path=str(output_path.resolve()),
            diagnostics=diagnostics,
        )
    except BaseException as exc:
        _atomic_write_json(
            output_path,
            {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "extension_id": "N0-N3",
                "status": "failed",
                "candidate": (
                    candidate.to_dict() if candidate is not None else None
                ),
                "error": {
                    "type": type(exc).__name__,
                    "message": str(exc),
                },
            },
        )
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, ResearchQARuntimeError):
            raise
        raise ResearchQARuntimeError(
            f"N0/N3 pre-quality runtime failed: {exc}"
        ) from exc


def run_researchqa_extension_runtime(
    config: Mapping[str, Any],
    run_root: str | Path,
    *,
    extension_id: str,
    embedding_factory: EmbeddingFactory = OllamaBatchEmbeddingClient,
    reranker_factory: RerankerFactory = Qwen3RerankerTransformersAdapter,
    extension_runner: ExtensionRunner = run_extension_candidate,
) -> ResearchQAExtensionRuntimeResult:
    """Run one approved extension with only its required model lifecycle."""

    if extension_id not in {"F2", "R1", "RR1", "S1"}:
        raise ResearchQARuntimeError(
            f"unsupported extension {extension_id!r}; "
            "expected F2, R1, RR1, or S1"
        )

    root = Path(run_root).resolve(strict=False)
    extension_root = root / "sweep" / "extensions" / extension_id
    runtime_root = extension_root / "runtime"
    preflight_path = runtime_root / "model-preflight.json"
    prequality_path = runtime_root / "prequality.json"
    summary_path = runtime_root / "runtime-summary.json"
    embedding_cache_dir = root / "model-cache" / "embeddings"
    hf_cache_dir = root / "model-cache" / "hf-cache"

    embedding: object | None = None
    reranker: object | None = None
    embedding_preflight: Mapping[str, Any] | None = None
    reranker_preflight: Mapping[str, Any] | None = None
    embedding_released = False
    embedding_cache_only = False
    reranker_released = False
    record: SweepCandidateRecord | None = None
    candidate: StrategyCandidate | None = None
    baseline: StrategyCandidate | None = None
    diagnostics: Mapping[str, object] | None = None
    diagnostic_key: str | None = None
    note_route_diagnostics: Mapping[str, object] | None = None
    input_summary: dict[str, Any] = {}

    def write_preflight(
        *,
        status: str,
        error: BaseException | None = None,
    ) -> None:
        _atomic_write_json(
            preflight_path,
            {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "extension_id": extension_id,
                "status": status,
                "embedding": embedding_preflight,
                "reranker": (
                    reranker_preflight
                    if extension_id == "RR1"
                    else {
                        "required": False,
                        "reason": "extension-candidate-rerank-off",
                    }
                ),
                "lifecycle": {
                    "embedding_released": embedding_released,
                    "embedding_cache_only": embedding_cache_only,
                    "reranker_required": extension_id == "RR1",
                    "reranker_released": reranker_released,
                },
                "error": (
                    {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                    if error is not None
                    else None
                ),
            },
        )

    def before_extension_rerank_stage() -> None:
        nonlocal embedding_released
        nonlocal embedding_cache_only
        nonlocal reranker_preflight
        if extension_id != "RR1" or embedding is None or reranker is None:
            raise ResearchQARuntimeError(
                "RR1 model lifecycle is not initialized"
            )
        if embedding_cache_only or reranker_preflight is not None:
            raise ResearchQARuntimeError(
                "RR1 model lifecycle may only enter once"
            )
        embedding.release_model()
        embedding_released = True
        embedding.enter_cache_only()
        if getattr(embedding, "_cache_only", False) is not True:
            raise ResearchQARuntimeError(
                "RR1 requires the embedding client to enter cache-only"
            )
        embedding_cache_only = True
        reranker_preflight = _preflight_payload(reranker.preflight())
        write_preflight(status="models-preflighted")

    def assert_extension_embedding_cache_only(
        _candidate: object,
    ) -> None:
        if (
            not embedding_cache_only
            or embedding is None
            or getattr(embedding, "_cache_only", False) is not True
            or reranker_preflight is None
        ):
            raise ResearchQARuntimeError(
                "RR1 reranking requires preflighted cache-only models"
            )

    failure: BaseException | None = None
    try:
        _validate_model_config(config)
        questions, paper_ids, question_path = load_suite_questions(config)
        frozen_notes, manifest_path = load_frozen_notes(
            root,
            expected_paper_ids=paper_ids,
        )
        documents = load_main_documents(
            root,
            expected_paper_ids=paper_ids,
        )
        if len(documents) != EXPECTED_PAPER_COUNT:
            raise ResearchQARuntimeError(
                f"runtime requires exactly {EXPECTED_PAPER_COUNT} Main documents"
            )
        if extension_id == "F2":
            candidate, baseline = _f2_candidates(config)
            diagnostics = _f2_prequality(documents)
            diagnostic_key = "pdf_chunking"
        elif extension_id == "RR1":
            candidate, baseline = _rr1_candidates(config)
            diagnostics = dict(RR1_RERANK_FUSION_POLICY)
            diagnostic_key = "rerank_fusion"
        elif extension_id == "R1":
            candidate, baseline = _r1_candidates(config)
            diagnostics = dict(R1_RETRIEVER_FUSION_POLICY)
            diagnostic_key = "retriever_fusion"
        else:
            candidate, baseline = _s1_candidates(config)
            diagnostics = dict(S1_SOURCE_FUSION_POLICY)
            diagnostic_key = "source_fusion"
            note_route_diagnostics = _s1_note_prequality(
                candidate,
                documents,
                frozen_notes,
            )
        _atomic_write_json(
            prequality_path,
            {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "extension_id": extension_id,
                "candidate_config_id": candidate.config_id,
                "status": "completed",
                "diagnostics": diagnostics,
                **(
                    {"note_route": note_route_diagnostics}
                    if note_route_diagnostics is not None
                    else {}
                ),
            },
        )
        input_summary = {
            "questions_path": str(question_path),
            "frozen_notes_manifest_path": str(manifest_path),
            "retrieval_scope": config["retrieval"]["scope"],
            "paper_count": len(documents),
            "question_count": len(questions),
            "paper_ids": list(paper_ids),
        }

        embedding = embedding_factory(cache_dir=embedding_cache_dir)
        embedding_preflight = _preflight_payload(embedding.preflight())
        write_preflight(status="embedding-preflighted")
        if extension_id == "RR1":
            reranker = reranker_factory(
                hf_home=hf_cache_dir,
                device="cuda",
            )

        record = extension_runner(
            config=config,
            run_root=root,
            documents=documents,
            questions=questions,
            frozen_notes=frozen_notes,
            embedder=embedding,
            reranker=reranker,
            extension_id=extension_id,
            candidate=candidate,
            baseline_candidate=baseline,
            before_rerank_stage=(
                before_extension_rerank_stage
                if extension_id == "RR1"
                else None
            ),
            assert_embedding_cache_only=(
                assert_extension_embedding_cache_only
                if extension_id == "RR1"
                else None
            ),
        )
        if not isinstance(record, SweepCandidateRecord):
            raise ResearchQARuntimeError(
                "extension runner must return SweepCandidateRecord"
            )
        if record.candidate != candidate:
            raise ResearchQARuntimeError(
                "extension runner returned a different candidate identity"
            )
        if not record.is_complete(
            expected_paper_ids=paper_ids,
            expected_question_ids=tuple(
                str(question["row_id"]) for question in questions
            ),
        ):
            raise ResearchQARuntimeError(
                f"extension {extension_id} did not complete the frozen input set: "
                f"status={record.status}, error={record.error}"
            )
        if not record.guardrail_finalized:
            raise ResearchQARuntimeError(
                f"extension {extension_id} completed without finalized guardrails"
            )
        if (
            len(record.evaluable_set) != EXPECTED_MAPPED_GROUP_COUNT
            or len({row_id for row_id, _group_id in record.evaluable_set})
            != EXPECTED_EVALUABLE_QUESTION_COUNT
        ):
            raise ResearchQARuntimeError(
                f"extension {extension_id} must preserve "
                f"{EXPECTED_EVALUABLE_QUESTION_COUNT} evaluable questions and "
                f"{EXPECTED_MAPPED_GROUP_COUNT} mapped groups"
            )
        corpus_diagnostics = record.payload.get("corpus_diagnostics")
        if (
            not isinstance(corpus_diagnostics, Mapping)
            or diagnostic_key is None
            or corpus_diagnostics.get(diagnostic_key) != diagnostics
        ):
            raise ResearchQARuntimeError(
                f"extension result diagnostics differ from {extension_id} "
                "pre-quality"
            )
        if (
            extension_id == "S1"
            and corpus_diagnostics.get("note_route")
            != note_route_diagnostics
        ):
            raise ResearchQARuntimeError(
                "extension result note route differs from S1 pre-quality"
            )
    except BaseException as exc:
        failure = exc
    finally:
        if embedding is not None and not embedding_released:
            try:
                embedding.release_model()
                embedding_released = True
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if reranker is not None:
            try:
                reranker.release_model()
                reranker_released = True
            except BaseException as exc:
                if failure is None:
                    failure = exc

    if failure is not None:
        write_preflight(status="failed", error=failure)
        _atomic_write_json(
            summary_path,
            {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "extension_id": extension_id,
                "status": "failed",
                "run_root": str(root),
                "inputs": input_summary,
                "candidate": (
                    candidate.to_dict() if candidate is not None else None
                ),
                "baseline": (
                    baseline.to_dict() if baseline is not None else None
                ),
                "prequality_path": str(prequality_path.resolve()),
                "model_preflight_path": str(preflight_path.resolve()),
                "error": {
                    "type": type(failure).__name__,
                    "message": str(failure),
                },
            },
        )
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise failure
        if isinstance(failure, ResearchQARuntimeError):
            raise failure
        raise ResearchQARuntimeError(
            f"ResearchQA extension runtime failed: {failure}"
        ) from failure

    assert record is not None
    assert candidate is not None
    assert baseline is not None
    assert diagnostics is not None
    assert diagnostic_key is not None
    write_preflight(status="completed")
    _atomic_write_json(
        summary_path,
        {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "extension_id": extension_id,
            "status": "completed",
            "run_root": str(root),
            "inputs": input_summary,
            "candidate": candidate.to_dict(),
            "baseline": baseline.to_dict(),
            "model_cache": {
                "embeddings": str(embedding_cache_dir.resolve()),
                "huggingface": (
                    str(hf_cache_dir.resolve())
                    if extension_id == "RR1"
                    else None
                ),
            },
            "lifecycle": {
                "embedding_released": embedding_released,
                "embedding_cache_only": embedding_cache_only,
                "reranker_required": extension_id == "RR1",
                "reranker_released": reranker_released,
            },
            "prequality_path": str(prequality_path.resolve()),
            "model_preflight_path": str(preflight_path.resolve()),
            "result": {
                "status": record.status,
                "result_path": record.result_path,
                "resumed": record.resumed,
                "primary_score": record.primary,
                "guardrails_passed": record.guardrails_passed,
                "completed_paper_count": len(record.completed_paper_ids),
                "completed_question_count": len(
                    record.completed_question_ids
                ),
                "evaluable_question_count": len(
                    {
                        row_id
                        for row_id, _group_id in record.evaluable_set
                    }
                ),
                "mapped_group_count": len(record.evaluable_set),
                "chunk_count": record.chunk_count,
                "corpus_diagnostics": dict(
                    record.payload["corpus_diagnostics"]
                ),
            },
        },
    )
    return ResearchQAExtensionRuntimeResult(
        extension_id=extension_id,
        record=record,
        model_preflight_path=str(preflight_path.resolve()),
        prequality_path=str(prequality_path.resolve()),
        runtime_summary_path=str(summary_path.resolve()),
    )


def run_researchqa_runtime(
    config: Mapping[str, Any],
    run_root: str | Path,
    *,
    embedding_factory: EmbeddingFactory = OllamaBatchEmbeddingClient,
    reranker_factory: RerankerFactory = Qwen3RerankerTransformersAdapter,
    sweep_runner: SweepRunner = run_strategy_sweep,
) -> ResearchQARuntimeResult:
    """Validate live inputs, enforce model lifecycle, and run the full sweep."""

    root = Path(run_root).resolve(strict=False)
    runtime_root = root / "runtime"
    preflight_path = runtime_root / "model-preflight.json"
    summary_path = runtime_root / "runtime-summary.json"
    embedding_cache_dir = root / "model-cache" / "embeddings"
    hf_cache_dir = root / "model-cache" / "hf-cache"

    embedding: object | None = None
    reranker: object | None = None
    embedding_preflight: Mapping[str, Any] | None = None
    reranker_preflight: Mapping[str, Any] | None = None
    embedding_released = False
    cache_only_entered = False
    reranker_preflighted = False
    sweep_result: StrategySweepResult | None = None
    input_summary: dict[str, Any] = {}

    def write_preflight(*, status: str, error: BaseException | None = None) -> None:
        _atomic_write_json(
            preflight_path,
            {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "status": status,
                "embedding": embedding_preflight,
                "reranker": reranker_preflight,
                "lifecycle": {
                    "embedding_released": embedding_released,
                    "embedding_cache_only": cache_only_entered,
                    "reranker_preflighted": reranker_preflighted,
                },
                "error": (
                    {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                    if error is not None
                    else None
                ),
            },
        )

    def before_rerank_stage() -> None:
        nonlocal embedding_released
        nonlocal cache_only_entered
        nonlocal reranker_preflight
        nonlocal reranker_preflighted
        if embedding is None or reranker is None:
            raise ResearchQARuntimeError("runtime models are not initialized")
        if cache_only_entered or reranker_preflighted:
            raise ResearchQARuntimeError(
                "before_rerank_stage may only run once"
            )
        embedding.release_model()
        embedding_released = True
        embedding.enter_cache_only()
        if getattr(embedding, "_cache_only", False) is not True:
            raise ResearchQARuntimeError(
                "embedding client did not enter cache-only mode"
            )
        cache_only_entered = True
        reranker_preflight = _preflight_payload(reranker.preflight())
        reranker_preflighted = True
        write_preflight(status="completed")

    def assert_embedding_cache_only(_candidate: object) -> None:
        if (
            not cache_only_entered
            or embedding is None
            or getattr(embedding, "_cache_only", False) is not True
        ):
            raise ResearchQARuntimeError(
                "reranking requires the embedding client to be cache-only"
            )

    failure: BaseException | None = None
    try:
        _validate_model_config(config)
        questions, paper_ids, question_path = load_suite_questions(config)
        frozen_notes, manifest_path = load_frozen_notes(
            root,
            expected_paper_ids=paper_ids,
        )
        documents = load_main_documents(
            root,
            expected_paper_ids=paper_ids,
        )
        if len(documents) != EXPECTED_PAPER_COUNT:
            raise ResearchQARuntimeError(
                f"runtime requires exactly {EXPECTED_PAPER_COUNT} Main documents"
            )
        input_summary = {
            "questions_path": str(question_path),
            "frozen_notes_manifest_path": str(manifest_path),
            "retrieval_scope": config["retrieval"]["scope"],
            "paper_count": len(documents),
            "question_count": len(questions),
            "paper_ids": list(paper_ids),
        }

        embedding = embedding_factory(cache_dir=embedding_cache_dir)
        reranker = reranker_factory(hf_home=hf_cache_dir, device="cuda")
        embedding_preflight = _preflight_payload(embedding.preflight())
        write_preflight(status="embedding-preflighted")

        sweep_result = sweep_runner(
            config=config,
            run_root=root,
            documents=documents,
            questions=questions,
            frozen_notes=frozen_notes,
            embedder=embedding,
            reranker=reranker,
            before_rerank_stage=before_rerank_stage,
            assert_embedding_cache_only=assert_embedding_cache_only,
        )
        if not isinstance(sweep_result, StrategySweepResult):
            raise ResearchQARuntimeError(
                "sweep runner must return StrategySweepResult"
            )
    except BaseException as exc:
        failure = exc
    finally:
        if embedding is not None and not embedding_released:
            try:
                embedding.release_model()
                embedding_released = True
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if reranker is not None:
            try:
                reranker.release_model()
            except BaseException as exc:
                if failure is None:
                    failure = exc

    if failure is not None:
        write_preflight(status="failed", error=failure)
        _atomic_write_json(
            summary_path,
            {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "status": "failed",
                "run_root": str(root),
                "inputs": input_summary,
                "model_preflight_path": str(preflight_path.resolve()),
                "error": {
                    "type": type(failure).__name__,
                    "message": str(failure),
                },
            },
        )
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise failure
        if isinstance(failure, ResearchQARuntimeError):
            raise failure
        raise ResearchQARuntimeError(
            f"ResearchQA runtime failed: {failure}"
        ) from failure

    assert sweep_result is not None
    write_preflight(
        status="completed" if reranker_preflighted else "embedding-only"
    )
    _atomic_write_json(
        summary_path,
        {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "status": "completed",
            "run_root": str(root),
            "inputs": input_summary,
            "model_cache": {
                "embeddings": str(embedding_cache_dir.resolve()),
                "huggingface": str(hf_cache_dir.resolve()),
            },
            "lifecycle": {
                "embedding_released": embedding_released,
                "embedding_cache_only": cache_only_entered,
                "reranker_preflighted": reranker_preflighted,
                "reranker_released": True,
            },
            "model_preflight_path": str(preflight_path.resolve()),
            "sweep": _sweep_summary(sweep_result),
        },
    )
    return ResearchQARuntimeResult(
        sweep_result=sweep_result,
        model_preflight_path=str(preflight_path.resolve()),
        runtime_summary_path=str(summary_path.resolve()),
    )


__all__ = [
    "EXPECTED_PAPER_COUNT",
    "EXPECTED_QUESTION_COUNT",
    "EXPECTED_EVALUABLE_QUESTION_COUNT",
    "EXPECTED_MAPPED_GROUP_COUNT",
    "RUNTIME_SCHEMA_VERSION",
    "ResearchQARuntimeError",
    "ResearchQAExtensionRuntimeResult",
    "ResearchQANotePrequalityResult",
    "ResearchQARuntimeResult",
    "load_frozen_notes",
    "load_suite_questions",
    "run_researchqa_extension_runtime",
    "run_n0_n3_prequality_runtime",
    "run_researchqa_runtime",
]
