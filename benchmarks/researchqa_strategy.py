"""Pure strategy orchestration for the ResearchQA rq-2 live adapter.

This module connects the existing native-source, chunking, retrieval, and
scoring primitives without loading models or touching production state.  Live
callers inject a batch embedder and the already-defined reranker adapter.
"""

from __future__ import annotations

import json
import math
import os
import time
import unicodedata
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import (
    Any,
    Mapping,
    MutableMapping,
    Protocol,
    Sequence,
    runtime_checkable,
)
from urllib.parse import urlparse

from benchmarks.researchqa_chunking import (
    NOTE_CHUNKER_IDS,
    PDF_CHUNKER_IDS,
    NoteChunk,
    ResearchQAChunk,
    chunk_note,
    chunk_pdf,
    note_chunk_pdf_backlinks,
)
from benchmarks.researchqa_retrieval import (
    BM25Index,
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
    SOURCE_COMPOSITION_IDS,
    RerankerAdapter,
    RetrievalHit,
    exact_cosine_search,
    hierarchical_pdf,
    note_guided_pdf,
    note_to_pdf,
    pdf_note_rrf,
    pdf_only,
    reciprocal_rank_fusion,
    rerank_hits,
)
from benchmarks.researchqa_scoring import (
    CandidateSummary,
    EvidenceMapping,
    MacroAggregate,
    MappingCoverage,
    QuestionScore,
    canonical_fingerprint,
    evaluate_mapping_coverage,
    macro_aggregate,
    map_reference_groups,
    rank_candidates,
    score_ranking,
)
from service.pdf_ir import CanonicalDocument, DocumentPage, hash_text


REFERENCE_MATCH_REVISION = "nfkc-whitespace-partial-sequence-v1"
REFERENCE_EXACT_METHOD = "nfkc-whitespace-exact-v1"
DEFAULT_FUZZY_THRESHOLD = 0.86
DEFAULT_MAPPING_WORKERS = 8
PARALLEL_MAPPING_MIN_QUESTIONS = 32
RETRIEVER_IDS = ("dense", "bm25", "hybrid-rrf")
RERANKER_IDS = (
    "rerank-off",
    "rerank-20-to-10",
    "rerank-50-to-10",
    "rerank-100-to-10",
)
STAGE_IDS = (
    "pdf-chunker",
    "note-chunker",
    "retriever",
    "source-composition",
    "reranker",
    "top2-confirmation",
)
NOTE_COMPOSITIONS = frozenset(
    {"note-to-pdf", "pdf-note-rrf", "note-guided-pdf"}
)


class StrategyContractError(ValueError):
    """Raised when a strategy input cannot produce a valid comparable run."""


@runtime_checkable
class EmbedderAdapter(Protocol):
    """Narrow batch embedding boundary implemented by the live model layer."""

    def embed_texts(
        self,
        texts: Sequence[str],
    ) -> Sequence[Sequence[float]]:
        """Return one finite, non-zero vector per input text."""


def normalize_paper_id(value: object) -> str:
    """Normalize an OpenAlex URL or already-normalized paper identifier."""

    if not isinstance(value, str) or not value.strip():
        raise StrategyContractError("paper_id must be a non-empty string")
    text = value.strip()
    parsed = urlparse(text)
    if parsed.scheme:
        text = parsed.path.rstrip("/").rsplit("/", 1)[-1]
    if not text or "/" in text or "\\" in text:
        raise StrategyContractError(f"unsupported paper_id: {value!r}")
    return text


def _read_jsonl(path: Path) -> tuple[dict[str, Any], ...]:
    records: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError as exc:
        raise StrategyContractError(f"native IR does not exist: {path}") from exc
    for line_number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise StrategyContractError(
                f"{path}:{line_number}: invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(value, dict):
            raise StrategyContractError(
                f"{path}:{line_number}: native IR row must be an object"
            )
        records.append(value)
    return tuple(records)


def load_main_document(native_ir_path: str | Path) -> CanonicalDocument:
    """Load one paper's Main PDF native units as a canonical page document."""

    path = Path(native_ir_path)
    records = [
        record
        for record in _read_jsonl(path)
        if record.get("file_id") == "Main"
        and record.get("media_type") == "application/pdf"
        and record.get("source_role") == "benchmark_pdf"
    ]
    if not records:
        raise StrategyContractError(f"{path}: no Main PDF native units")

    paper_ids = {normalize_paper_id(record.get("paper_id")) for record in records}
    source_hashes = {record.get("source_sha256") for record in records}
    parser_fingerprints = {
        record.get("parser_fingerprint") for record in records
    }
    if len(paper_ids) != 1:
        raise StrategyContractError(f"{path}: Main units span multiple papers")
    if len(source_hashes) != 1 or not _is_sha256(next(iter(source_hashes))):
        raise StrategyContractError(f"{path}: inconsistent Main source SHA-256")
    if (
        len(parser_fingerprints) != 1
        or not _is_sha256(next(iter(parser_fingerprints)))
    ):
        raise StrategyContractError(
            f"{path}: inconsistent Main parser fingerprint"
        )

    ordered = sorted(records, key=lambda record: int(record.get("ordinal", -1)))
    expected_ordinals = list(range(1, len(ordered) + 1))
    actual_ordinals = [int(record.get("ordinal", -1)) for record in ordered]
    if actual_ordinals != expected_ordinals:
        raise StrategyContractError(
            f"{path}: Main native ordinals must be contiguous and one-based"
        )

    paper_id = next(iter(paper_ids))
    pages: list[DocumentPage] = []
    for expected_page, record in enumerate(ordered, 1):
        coordinate = record.get("coordinate")
        if (
            not isinstance(coordinate, Mapping)
            or coordinate.get("coordinate_type") != "pdf_page"
            or coordinate.get("page") != expected_page
        ):
            raise StrategyContractError(
                f"{path}: Main coordinate {expected_page} is not pdf_page "
                f"{expected_page}"
            )
        text = record.get("text")
        if not isinstance(text, str):
            raise StrategyContractError(
                f"{path}: Main page {expected_page} text must be a string"
            )
        if record.get("text_sha256") != hash_text(text):
            raise StrategyContractError(
                f"{path}: Main page {expected_page} text hash mismatch"
            )
        pages.append(
            DocumentPage.create(
                paper_id=paper_id,
                file_id="Main",
                pdf_page_index=expected_page - 1,
                text=text,
            )
        )
    return CanonicalDocument(
        paper_id=paper_id,
        file_id="Main",
        file_hash=str(next(iter(source_hashes))),
        extractor_fingerprint=str(next(iter(parser_fingerprints))),
        pages=tuple(pages),
    )


def load_main_documents(
    run_root: str | Path,
    *,
    expected_paper_ids: Sequence[str] | None = None,
) -> dict[str, CanonicalDocument]:
    """Load every ``run_root/source/<paper>/native-ir.jsonl`` Main document."""

    source_root = Path(run_root).resolve(strict=False) / "source"
    if not source_root.is_dir():
        raise StrategyContractError(f"source directory does not exist: {source_root}")
    documents: dict[str, CanonicalDocument] = {}
    for path in sorted(source_root.glob("*/native-ir.jsonl")):
        document = load_main_document(path)
        if document.paper_id in documents:
            raise StrategyContractError(
                f"duplicate Main document for {document.paper_id}"
            )
        if path.parent.name != document.paper_id:
            raise StrategyContractError(
                f"{path}: parent directory must match paper_id "
                f"{document.paper_id!r}"
            )
        documents[document.paper_id] = document
    if not documents:
        raise StrategyContractError(f"no per-paper native IR found under {source_root}")
    if expected_paper_ids is not None:
        expected = {normalize_paper_id(value) for value in expected_paper_ids}
        if set(documents) != expected:
            raise StrategyContractError(
                "Main document set differs from expected papers: "
                f"missing={sorted(expected - set(documents))}, "
                f"unexpected={sorted(set(documents) - expected)}"
            )
    return dict(sorted(documents.items()))


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def normalize_reference_text(text: str) -> str:
    """Apply the versioned NFKC + whitespace reference normalization."""

    if not isinstance(text, str) or not text.strip():
        raise StrategyContractError("reference text must be non-empty")
    return " ".join(unicodedata.normalize("NFKC", text).split())


def _partial_sequence_ratio(left: str, right: str) -> float:
    """Dependency-free deterministic partial SequenceMatcher ratio."""

    if not left or not right:
        return 0.0
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    matcher = SequenceMatcher(None, shorter, longer, autojunk=False)
    best = matcher.ratio()
    for block in matcher.get_matching_blocks():
        start = max(0, block.b - block.a)
        window = longer[start : start + len(shorter)]
        best = max(
            best,
            SequenceMatcher(None, shorter, window, autojunk=False).ratio(),
        )
        if math.isclose(best, 1.0):
            return 1.0
    return best


@dataclass(frozen=True)
class UnmappedEvidenceGroup:
    row_id: str
    paper_id: str
    group_id: str
    alternatives: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "row_id": self.row_id,
            "paper_id": self.paper_id,
            "group_id": self.group_id,
            "alternatives": list(self.alternatives),
        }


@dataclass(frozen=True)
class EvidenceMappingBundle:
    revision: str
    fuzzy_threshold: float
    mappings: tuple[EvidenceMapping, ...]
    unmapped: tuple[UnmappedEvidenceGroup, ...]
    coverage: MappingCoverage

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "revision": self.revision,
            "fuzzy_threshold": self.fuzzy_threshold,
            "coverage": self.coverage.to_dict(),
            "mappings": [mapping.to_dict() for mapping in self.mappings],
            "unmapped": [item.to_dict() for item in self.unmapped],
        }


def map_question_references(
    question: Mapping[str, Any],
    chunks: Sequence[ResearchQAChunk],
    *,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> EvidenceMapping:
    """Map one question's AND/OR reference groups to stable chunk IDs."""

    if not 0.0 <= fuzzy_threshold <= 1.0:
        raise StrategyContractError("fuzzy_threshold must be in [0, 1]")
    paper_id = normalize_paper_id(question.get("paper_id"))
    paper_chunks = tuple(
        sorted(
            (chunk for chunk in chunks if chunk.paper_id == paper_id),
            key=lambda chunk: chunk.chunk_id,
        )
    )
    normalized_chunks = {
        chunk.chunk_id: normalize_reference_text(chunk.text)
        for chunk in paper_chunks
    }
    return _map_question_with_normalized_chunks(
        question,
        normalized_chunks,
        fuzzy_threshold=fuzzy_threshold,
        reference_cache={},
    )


def _map_question_with_normalized_chunks(
    question: Mapping[str, Any],
    normalized_chunks: Mapping[str, str],
    *,
    fuzzy_threshold: float,
    reference_cache: MutableMapping[
        str,
        Mapping[str, object] | None,
    ],
) -> EvidenceMapping:
    row_id = _required_text(question, "row_id")
    paper_id = normalize_paper_id(question.get("paper_id"))
    domain = _required_text(question, "domain")
    question_type = _required_text(question, "question_type")
    references = question.get("expected_references")
    if not isinstance(references, list):
        raise StrategyContractError(
            f"{row_id}: expected_references must be a list"
        )

    def mapper(reference: str) -> Mapping[str, object] | None:
        normalized = normalize_reference_text(reference)
        if normalized in reference_cache:
            return reference_cache[normalized]
        exact = tuple(
            chunk_id
            for chunk_id, chunk_text in normalized_chunks.items()
            if normalized in chunk_text
        )
        if exact:
            result: Mapping[str, object] | None = {
                "mapped_item_ids": exact,
                "match_method": REFERENCE_EXACT_METHOD,
                "match_score": 1.0,
            }
        else:
            scores = {
                chunk_id: _partial_sequence_ratio(normalized, chunk_text)
                for chunk_id, chunk_text in normalized_chunks.items()
            }
            if not scores:
                result = None
            else:
                best = max(scores.values())
                if best < fuzzy_threshold:
                    result = None
                else:
                    matches = tuple(
                        chunk_id
                        for chunk_id, score in sorted(scores.items())
                        if math.isclose(
                            score,
                            best,
                            rel_tol=0.0,
                            abs_tol=1e-12,
                        )
                    )
                    result = {
                        "mapped_item_ids": matches,
                        "match_method": REFERENCE_MATCH_REVISION,
                        "match_score": best,
                    }
        reference_cache[normalized] = result
        return result

    return map_reference_groups(
        row_id=row_id,
        paper_id=paper_id,
        domain=domain,
        question_type=question_type,
        reference_groups=references,
        mapper=mapper,
    )


def _map_paper_reference_batch(
    payload: tuple[
        tuple[Mapping[str, Any], ...],
        tuple[ResearchQAChunk, ...],
        float,
    ],
) -> tuple[EvidenceMapping, ...]:
    questions, chunks, fuzzy_threshold = payload
    paper_id = normalize_paper_id(questions[0].get("paper_id"))
    normalized_chunks = {
        chunk.chunk_id: normalize_reference_text(chunk.text)
        for chunk in sorted(chunks, key=lambda item: item.chunk_id)
        if chunk.paper_id == paper_id
    }
    reference_cache: dict[str, Mapping[str, object] | None] = {}
    return tuple(
        _map_question_with_normalized_chunks(
            question,
            normalized_chunks,
            fuzzy_threshold=fuzzy_threshold,
            reference_cache=reference_cache,
        )
        for question in questions
    )


def map_all_references(
    questions: Sequence[Mapping[str, Any]],
    chunks: Sequence[ResearchQAChunk],
    *,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    overall_minimum: float = 0.95,
    per_paper_minimum: float = 0.90,
    mapping_workers: int | None = None,
) -> EvidenceMappingBundle:
    """Map all questions and retain an explicit unmapped-group ledger."""

    row_ids = [_required_text(question, "row_id") for question in questions]
    if len(row_ids) != len(set(row_ids)):
        raise StrategyContractError("question row_ids must be unique")
    if not 0.0 <= fuzzy_threshold <= 1.0:
        raise StrategyContractError("fuzzy_threshold must be in [0, 1]")
    if mapping_workers is not None and mapping_workers < 1:
        raise StrategyContractError("mapping_workers must be at least 1")
    questions_by_paper: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for question in questions:
        questions_by_paper[
            normalize_paper_id(question.get("paper_id"))
        ].append(question)
    chunks_by_paper: dict[str, list[ResearchQAChunk]] = defaultdict(list)
    for chunk in chunks:
        chunks_by_paper[chunk.paper_id].append(chunk)
    if mapping_workers is None:
        mapping_workers = (
            min(DEFAULT_MAPPING_WORKERS, os.cpu_count() or 1, len(questions))
            if len(questions) >= PARALLEL_MAPPING_MIN_QUESTIONS
            else 1
        )
    jobs = []
    for paper_id, paper_questions in questions_by_paper.items():
        batch_count = min(mapping_workers, len(paper_questions))
        batch_size = math.ceil(len(paper_questions) / batch_count)
        paper_chunks = tuple(chunks_by_paper.get(paper_id, ()))
        jobs.extend(
            (
                tuple(paper_questions[start : start + batch_size]),
                paper_chunks,
                fuzzy_threshold,
            )
            for start in range(0, len(paper_questions), batch_size)
        )
    job_batch = tuple(jobs)
    if mapping_workers == 1 or len(job_batch) <= 1:
        batches = tuple(_map_paper_reference_batch(job) for job in job_batch)
    else:
        with ProcessPoolExecutor(
            max_workers=min(mapping_workers, len(job_batch))
        ) as executor:
            batches = tuple(
                executor.map(_map_paper_reference_batch, job_batch)
            )
    mapping_by_row = {
        mapping.row_id: mapping for batch in batches for mapping in batch
    }
    mappings = tuple(mapping_by_row[row_id] for row_id in row_ids)
    unmapped = tuple(
        UnmappedEvidenceGroup(
            row_id=mapping.row_id,
            paper_id=mapping.paper_id,
            group_id=group.group_id,
            alternatives=tuple(
                alternative.reference_text for alternative in group.alternatives
            ),
        )
        for mapping in mappings
        for group in mapping.groups
        if not group.mapped
    )
    coverage = evaluate_mapping_coverage(
        mappings,
        overall_minimum=overall_minimum,
        per_paper_minimum=per_paper_minimum,
    )
    return EvidenceMappingBundle(
        revision=REFERENCE_MATCH_REVISION,
        fuzzy_threshold=fuzzy_threshold,
        mappings=mappings,
        unmapped=unmapped,
        coverage=coverage,
    )


def _required_text(value: Mapping[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item.strip():
        raise StrategyContractError(f"{key} must be a non-empty string")
    return item.strip()


@dataclass(frozen=True)
class StrategyCandidate:
    stage_id: str
    config_id: str
    pdf_chunker: str
    note_chunker: str | None
    retriever: str
    source_composition: str
    reranker: str
    reranker_depth: int | None
    reranker_keep: int = 10
    rankable: bool = True

    def __post_init__(self) -> None:
        if self.stage_id not in STAGE_IDS:
            raise StrategyContractError(
                f"unsupported candidate stage: {self.stage_id}"
            )
        if not self.config_id:
            raise StrategyContractError("candidate config_id must be non-empty")
        _require_member(self.pdf_chunker, PDF_CHUNKER_IDS, "pdf_chunker")
        if self.note_chunker is not None:
            _require_member(self.note_chunker, NOTE_CHUNKER_IDS, "note_chunker")
        _require_member(self.retriever, RETRIEVER_IDS, "retriever")
        _require_member(
            self.source_composition,
            SOURCE_COMPOSITION_IDS,
            "source_composition",
        )
        _require_member(self.reranker, RERANKER_IDS, "reranker")
        if self.requires_notes and self.note_chunker is None:
            raise StrategyContractError(
                "note-dependent candidates require note_chunker"
            )
        if self.requires_parents and self.pdf_chunker != "pdf-parent-child":
            raise StrategyContractError(
                "hierarchical-pdf requires pdf-parent-child"
            )
        if self.reranker == "rerank-off" and self.reranker_depth is not None:
            raise StrategyContractError("rerank-off cannot set reranker_depth")
        if self.reranker != "rerank-off" and (
            self.reranker_depth is None or self.reranker_depth < 1
        ):
            raise StrategyContractError(
                "enabled rerankers require positive reranker_depth"
            )
        if self.reranker_keep < 1:
            raise StrategyContractError("reranker_keep must be positive")

    @property
    def requires_notes(self) -> bool:
        return (
            self.stage_id == "note-chunker"
            or self.source_composition in NOTE_COMPOSITIONS
        )

    @property
    def requires_parents(self) -> bool:
        return self.source_composition == "hierarchical-pdf"

    @property
    def fingerprint(self) -> str:
        return canonical_fingerprint(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            "stage_id": self.stage_id,
            "config_id": self.config_id,
            "pdf_chunker": self.pdf_chunker,
            "note_chunker": self.note_chunker,
            "retriever": self.retriever,
            "source_composition": self.source_composition,
            "reranker": self.reranker,
            "reranker_depth": self.reranker_depth,
            "reranker_keep": self.reranker_keep,
            "rankable": self.rankable,
        }


@dataclass(frozen=True)
class ConfirmationSelection:
    pdf_chunkers: tuple[str, ...]
    retrievers: tuple[str, ...]
    source_compositions: tuple[str, ...]
    reranker_modes: tuple[str, ...]


@dataclass(frozen=True)
class CandidatePlan:
    stages: Mapping[str, tuple[StrategyCandidate, ...]]

    @property
    def candidates(self) -> tuple[StrategyCandidate, ...]:
        return tuple(
            candidate
            for stage_id in STAGE_IDS
            for candidate in self.stages.get(stage_id, ())
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "stages": {
                stage_id: [
                    candidate.to_dict() for candidate in self.stages.get(stage_id, ())
                ]
                for stage_id in STAGE_IDS
            },
        }


def _candidate(
    stage_id: str,
    *,
    pdf_chunker: str,
    note_chunker: str | None,
    retriever: str,
    source_composition: str,
    reranker: str,
    reranker_options: Mapping[str, Mapping[str, Any]],
    rankable: bool = True,
) -> StrategyCandidate:
    components = {
        "stage_id": stage_id,
        "pdf_chunker": pdf_chunker,
        "note_chunker": note_chunker,
        "retriever": retriever,
        "source_composition": source_composition,
        "reranker": reranker,
    }
    config_id = f"{stage_id}-{canonical_fingerprint(components)[:20]}"
    option = reranker_options[reranker]
    return StrategyCandidate(
        config_id=config_id,
        reranker_depth=(
            int(option["input_k"]) if bool(option.get("enabled")) else None
        ),
        reranker_keep=int(option.get("output_k", 10)),
        rankable=rankable,
        **components,
    )


def generate_orthogonal_candidates(
    config: Mapping[str, Any],
    *,
    anchor_pdf_chunker: str = "pdf-fixed-800",
    anchor_note_chunker: str = "note-section",
    anchor_retriever: str = "dense",
    anchor_source_composition: str = "pdf-only",
    best_reranker: str = "rerank-50-to-10",
    confirmation: ConfirmationSelection | None = None,
) -> CandidatePlan:
    """Generate stage-wise orthogonal scans, never the global Cartesian product."""

    stages = config.get("stages")
    if not isinstance(stages, Mapping):
        raise StrategyContractError("config.stages must be a mapping")
    pdf_ids = _option_ids(stages, "pdf_chunkers", expected=PDF_CHUNKER_IDS)
    note_ids = _option_ids(stages, "note_chunkers", expected=NOTE_CHUNKER_IDS)
    retriever_ids = _option_ids(
        stages, "retrievers", expected=RETRIEVER_IDS
    )
    composition_ids = _option_ids(
        stages,
        "source_compositions",
        expected=SOURCE_COMPOSITION_IDS,
    )
    reranker_rows = _option_rows(stages, "rerankers")
    reranker_ids = tuple(str(row.get("id")) for row in reranker_rows)
    if set(reranker_ids) != set(RERANKER_IDS):
        raise StrategyContractError(
            f"rerankers must be exactly {list(RERANKER_IDS)}"
        )
    reranker_options = {str(row["id"]): row for row in reranker_rows}
    _require_member(anchor_pdf_chunker, pdf_ids, "anchor_pdf_chunker")
    _require_member(anchor_note_chunker, note_ids, "anchor_note_chunker")
    _require_member(anchor_retriever, retriever_ids, "anchor_retriever")
    _require_member(
        anchor_source_composition,
        composition_ids,
        "anchor_source_composition",
    )
    _require_member(best_reranker, reranker_ids, "best_reranker")

    off = "rerank-off"
    planned: dict[str, tuple[StrategyCandidate, ...]] = {}
    planned["pdf-chunker"] = tuple(
        _candidate(
            "pdf-chunker",
            pdf_chunker=pdf_id,
            note_chunker=None,
            retriever=anchor_retriever,
            source_composition="pdf-only",
            reranker=off,
            reranker_options=reranker_options,
        )
        for pdf_id in pdf_ids
    )
    planned["note-chunker"] = tuple(
        _candidate(
            "note-chunker",
            pdf_chunker=anchor_pdf_chunker,
            note_chunker=note_id,
            retriever=anchor_retriever,
            source_composition="pdf-note-rrf",
            reranker=off,
            reranker_options=reranker_options,
            rankable=note_id != "note-whole",
        )
        for note_id in note_ids
    )
    planned["retriever"] = tuple(
        _candidate(
            "retriever",
            pdf_chunker=anchor_pdf_chunker,
            note_chunker=None,
            retriever=retriever_id,
            source_composition="pdf-only",
            reranker=off,
            reranker_options=reranker_options,
        )
        for retriever_id in retriever_ids
    )
    planned["source-composition"] = tuple(
        _candidate(
            "source-composition",
            pdf_chunker=(
                "pdf-parent-child"
                if composition_id == "hierarchical-pdf"
                else anchor_pdf_chunker
            ),
            note_chunker=(
                anchor_note_chunker
                if composition_id in NOTE_COMPOSITIONS
                else None
            ),
            retriever=anchor_retriever,
            source_composition=composition_id,
            reranker=off,
            reranker_options=reranker_options,
        )
        for composition_id in composition_ids
    )
    planned["reranker"] = tuple(
        _candidate(
            "reranker",
            pdf_chunker=(
                "pdf-parent-child"
                if anchor_source_composition == "hierarchical-pdf"
                else anchor_pdf_chunker
            ),
            note_chunker=(
                anchor_note_chunker
                if anchor_source_composition in NOTE_COMPOSITIONS
                else None
            ),
            retriever=anchor_retriever,
            source_composition=anchor_source_composition,
            reranker=reranker_id,
            reranker_options=reranker_options,
        )
        for reranker_id in reranker_ids
    )

    confirmations: list[StrategyCandidate] = []
    if confirmation is not None:
        dimensions = (
            ("pdf_chunkers", confirmation.pdf_chunkers, pdf_ids),
            ("retrievers", confirmation.retrievers, retriever_ids),
            (
                "source_compositions",
                confirmation.source_compositions,
                composition_ids,
            ),
            ("reranker_modes", confirmation.reranker_modes, reranker_ids),
        )
        for label, selected, available in dimensions:
            if not 1 <= len(selected) <= 2 or len(set(selected)) != len(selected):
                raise StrategyContractError(
                    f"confirmation {label} must contain one or two unique IDs"
                )
            for value in selected:
                _require_member(value, available, f"confirmation.{label}")
        if "rerank-off" not in confirmation.reranker_modes:
            raise StrategyContractError(
                "confirmation reranker_modes must include rerank-off"
            )
        for pdf_id in confirmation.pdf_chunkers:
            for retriever_id in confirmation.retrievers:
                for composition_id in confirmation.source_compositions:
                    for reranker_id in confirmation.reranker_modes:
                        confirmations.append(
                            _candidate(
                                "top2-confirmation",
                                pdf_chunker=(
                                    "pdf-parent-child"
                                    if composition_id == "hierarchical-pdf"
                                    else pdf_id
                                ),
                                note_chunker=(
                                    anchor_note_chunker
                                    if composition_id in NOTE_COMPOSITIONS
                                    else None
                                ),
                                retriever=retriever_id,
                                source_composition=composition_id,
                                reranker=reranker_id,
                                reranker_options=reranker_options,
                            )
                        )
        maximum = int(
            stages.get("top2_confirmation", {}).get(
                "maximum_combinations", 16
            )
        )
        if len(confirmations) > min(16, maximum):
            raise StrategyContractError(
                "confirmation plan exceeds the approved maximum of 16"
            )
    planned["top2-confirmation"] = tuple(confirmations)
    return CandidatePlan(stages=planned)


def _option_rows(
    stages: Mapping[str, Any],
    key: str,
) -> tuple[Mapping[str, Any], ...]:
    raw = stages.get(key)
    if not isinstance(raw, list) or not raw:
        raise StrategyContractError(f"config.stages.{key} must be a non-empty list")
    if not all(isinstance(row, Mapping) and row.get("id") for row in raw):
        raise StrategyContractError(f"config.stages.{key} entries require id")
    ids = [str(row["id"]) for row in raw]
    if len(ids) != len(set(ids)):
        raise StrategyContractError(f"config.stages.{key} IDs must be unique")
    return tuple(raw)


def _option_ids(
    stages: Mapping[str, Any],
    key: str,
    *,
    expected: Sequence[str],
) -> tuple[str, ...]:
    ids = tuple(str(row["id"]) for row in _option_rows(stages, key))
    if set(ids) != set(expected):
        raise StrategyContractError(
            f"config.stages.{key} must be exactly {list(expected)}"
        )
    return ids


def _require_member(value: str, allowed: Sequence[str], label: str) -> None:
    if value not in allowed:
        raise StrategyContractError(f"{label} has unsupported value {value!r}")


@dataclass(frozen=True)
class QuestionStrategyResult:
    row_id: str
    paper_id: str
    domain: str
    question_type: str
    ranked_item_ids: tuple[str, ...]
    metrics: Mapping[str, float | None]

    def to_dict(self) -> dict[str, object]:
        return {
            "row_id": self.row_id,
            "paper_id": self.paper_id,
            "domain": self.domain,
            "question_type": self.question_type,
            "ranked_item_ids": list(self.ranked_item_ids),
            "metrics": dict(self.metrics),
        }


@dataclass(frozen=True)
class CandidateRunResult:
    candidate: StrategyCandidate
    question_results: tuple[QuestionStrategyResult, ...]
    aggregate: MacroAggregate
    mapping: EvidenceMappingBundle
    completed_paper_ids: tuple[str, ...]
    completed_question_ids: tuple[str, ...]
    p95_latency_ms: float
    index_bytes: int
    chunk_count: int
    guardrails_passed: bool
    latency_metrics: Mapping[str, object] = field(default_factory=dict)

    @property
    def primary_metric(self) -> str:
        if self.candidate.stage_id in {"pdf-chunker", "note-chunker"}:
            return "recall_at_5"
        return "coverage_ndcg_at_10"

    @property
    def primary_score(self) -> float:
        value = self.aggregate.overall.get(self.primary_metric)
        if value is None:
            raise StrategyContractError(
                f"{self.candidate.config_id}: no evaluable "
                f"{self.primary_metric} primary score"
            )
        return float(value)

    def is_complete(
        self,
        *,
        expected_paper_ids: Sequence[str],
        expected_question_ids: Sequence[str],
    ) -> bool:
        expected_papers = {
            normalize_paper_id(value) for value in expected_paper_ids
        }
        return (
            set(self.completed_paper_ids) == expected_papers
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
            primary=self.primary_score,
            p95_latency_ms=self.p95_latency_ms,
            index_bytes=self.index_bytes,
            chunk_count=self.chunk_count,
            complete=self.is_complete(
                expected_paper_ids=expected_paper_ids,
                expected_question_ids=expected_question_ids,
            ),
            guardrails_passed=(
                self.guardrails_passed
                and self.mapping.coverage.passed
                and self.candidate.rankable
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "candidate": self.candidate.to_dict(),
            "question_results": [
                result.to_dict() for result in self.question_results
            ],
            "score_summary": self.aggregate.to_dict(),
            "mapping": self.mapping.to_dict(),
            "completed_paper_ids": list(self.completed_paper_ids),
            "completed_question_ids": list(self.completed_question_ids),
            "primary_metric": self.primary_metric,
            "primary_score": self.primary_score,
            "p95_latency_ms": self.p95_latency_ms,
            "index_bytes": self.index_bytes,
            "chunk_count": self.chunk_count,
            "guardrails_passed": self.guardrails_passed,
            "latency_metrics": dict(self.latency_metrics),
        }


@dataclass(frozen=True)
class _CandidateCorpus:
    pdf_chunks: tuple[ResearchQAChunk, ...]
    pdf_parents: tuple[ResearchQAChunk, ...]
    note_chunks: tuple[NoteChunk, ...]
    note_backlinks: Mapping[str, tuple[str, ...]]


def _prepare_candidate_corpus(
    candidate: StrategyCandidate,
    documents: Mapping[str, CanonicalDocument],
    notes: Mapping[str, str] | None,
) -> _CandidateCorpus:
    pdf_chunks: list[ResearchQAChunk] = []
    pdf_parents: list[ResearchQAChunk] = []
    for paper_id, document in sorted(documents.items()):
        if document.paper_id != paper_id:
            raise StrategyContractError(
                f"document key differs from paper_id: {paper_id}"
            )
        result = chunk_pdf(document, candidate.pdf_chunker, is_main=True)
        if result.status != "completed":
            raise StrategyContractError(
                f"{paper_id}/{candidate.pdf_chunker}: {result.failure_reason}"
            )
        pdf_chunks.extend(result.chunks)
        pdf_parents.extend(result.parents)
    if candidate.requires_parents and not pdf_parents:
        raise StrategyContractError(
            "hierarchical-pdf requires pdf-parent-child parent chunks"
        )

    note_chunks: list[NoteChunk] = []
    if candidate.requires_notes:
        if candidate.note_chunker is None:
            raise StrategyContractError(
                f"{candidate.config_id}: note strategy has no note chunker"
            )
        if notes is None:
            raise StrategyContractError(
                f"{candidate.config_id}: frozen notes are required"
            )
        missing = sorted(set(documents) - set(notes))
        blank = sorted(
            paper_id
            for paper_id in documents
            if paper_id in notes and not notes[paper_id].strip()
        )
        if missing or blank:
            raise StrategyContractError(
                "note strategy cannot run without complete frozen notes: "
                f"missing={missing}, blank={blank}"
            )
        for paper_id in sorted(documents):
            result = chunk_note(
                notes[paper_id],
                candidate.note_chunker,
                paper_id=paper_id,
            )
            if result.status != "completed" or not result.chunks:
                raise StrategyContractError(
                    f"{paper_id}/{candidate.note_chunker}: "
                    f"{result.failure_reason or 'no note chunks'}"
                )
            note_chunks.extend(result.chunks)

    backlinks = {
        note_chunk.chunk_id: note_chunk_pdf_backlinks(
            note_chunk,
            tuple(
                chunk
                for chunk in pdf_chunks
                if chunk.paper_id == note_chunk.paper_id
            ),
        )
        for note_chunk in note_chunks
    }
    return _CandidateCorpus(
        pdf_chunks=tuple(pdf_chunks),
        pdf_parents=tuple(pdf_parents),
        note_chunks=tuple(note_chunks),
        note_backlinks=backlinks,
    )


def _embed_mapping(
    embedder: EmbedderAdapter,
    passages: Mapping[str, str],
    *,
    batch_size: int,
) -> dict[str, tuple[float, ...]]:
    if batch_size < 1:
        raise StrategyContractError("embedding batch_size must be positive")
    ids = tuple(sorted(passages))
    if not ids:
        return {}
    texts = tuple(passages[item_id] for item_id in ids)
    vectors_list: list[tuple[float, ...]] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        returned = tuple(embedder.embed_texts(batch))
        if len(returned) != len(batch):
            raise StrategyContractError(
                "embedder returned the wrong number of vectors"
            )
        vectors_list.extend(
            tuple(float(value) for value in vector)
            for vector in returned
        )
    vectors = tuple(vectors_list)
    if len(vectors) != len(ids):
        raise StrategyContractError(
            "embedder returned the wrong number of vectors"
        )
    dimensions = {len(vector) for vector in vectors}
    if len(dimensions) != 1 or next(iter(dimensions)) < 1:
        raise StrategyContractError("embedder dimensions must be non-empty and fixed")
    if any(
        not all(math.isfinite(value) for value in vector)
        or math.fsum(value * value for value in vector) == 0.0
        for vector in vectors
    ):
        raise StrategyContractError(
            "embedder vectors must be finite and non-zero"
        )
    return dict(zip(ids, vectors, strict=True))


@dataclass(frozen=True)
class _SearchIndex:
    passages: Mapping[str, str]
    embeddings: Mapping[str, tuple[float, ...]]
    bm25: BM25Index | None


def _make_search_index(
    passages: Mapping[str, str],
    *,
    retriever: str,
    embedder: EmbedderAdapter,
    embedding_batch_size: int,
) -> _SearchIndex:
    if not passages:
        return _SearchIndex({}, {}, None)
    embeddings = (
        _embed_mapping(
            embedder,
            passages,
            batch_size=embedding_batch_size,
        )
        if retriever in {"dense", "hybrid-rrf"}
        else {}
    )
    bm25 = (
        BM25Index(passages)
        if retriever in {"bm25", "hybrid-rrf"}
        else None
    )
    return _SearchIndex(dict(passages), embeddings, bm25)


def _search(
    index: _SearchIndex,
    *,
    retriever: str,
    query: str,
    query_embedding: Sequence[float] | None,
    top_k: int = 100,
) -> tuple[RetrievalHit, ...]:
    if not index.passages:
        return ()
    if retriever == "dense":
        if query_embedding is None:
            raise StrategyContractError("dense retrieval requires query embedding")
        return exact_cosine_search(
            query_embedding,
            index.embeddings,
            top_k=top_k,
        )
    if retriever == "bm25":
        assert index.bm25 is not None
        return index.bm25.search(query, top_k=top_k)
    if retriever == "hybrid-rrf":
        if query_embedding is None:
            raise StrategyContractError("hybrid retrieval requires query embedding")
        assert index.bm25 is not None
        return reciprocal_rank_fusion(
            (
                exact_cosine_search(
                    query_embedding,
                    index.embeddings,
                    top_k=top_k,
                ),
                index.bm25.search(query, top_k=top_k),
            ),
            top_k=top_k,
            source="hybrid-rrf",
        )
    raise StrategyContractError(f"unsupported retriever: {retriever}")


def _compose_hits(
    candidate: StrategyCandidate,
    *,
    pdf_hits: Sequence[RetrievalHit],
    note_hits: Sequence[RetrievalHit],
    parent_hits: Sequence[RetrievalHit],
    note_backlinks: Mapping[str, Sequence[str]],
    pdf_chunks: Mapping[str, ResearchQAChunk],
    top_k: int,
) -> tuple[RetrievalHit, ...]:
    if candidate.source_composition == "pdf-only":
        return pdf_only(pdf_hits, top_k=top_k)
    projected = note_to_pdf(note_hits, note_backlinks, top_k=top_k)
    if candidate.source_composition == "note-to-pdf":
        return projected
    if candidate.source_composition == "pdf-note-rrf":
        return pdf_note_rrf(pdf_hits, projected, top_k=top_k)
    if candidate.source_composition == "note-guided-pdf":
        return note_guided_pdf(
            note_hits,
            pdf_hits,
            note_backlinks,
            top_k=top_k,
        )
    if candidate.source_composition == "hierarchical-pdf":
        children_by_parent: dict[str, list[RetrievalHit]] = defaultdict(list)
        for hit in pdf_hits:
            parent_id = pdf_chunks[hit.item_id].parent_chunk_id
            if parent_id is not None:
                children_by_parent[parent_id].append(hit)
        return hierarchical_pdf(
            parent_hits,
            children_by_parent,
            top_k=top_k,
        )
    raise StrategyContractError(
        f"unsupported source composition: {candidate.source_composition}"
    )


def run_complete_candidate(
    candidate: StrategyCandidate,
    documents: Mapping[str, CanonicalDocument],
    questions: Sequence[Mapping[str, Any]],
    *,
    expected_paper_ids: Sequence[str],
    expected_question_ids: Sequence[str],
    embedder: EmbedderAdapter,
    reranker: RerankerAdapter | None = None,
    notes: Mapping[str, str] | None = None,
    embedding_batch_size: int = 64,
    reranker_batch_size: int = 8,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    mapping_overall_minimum: float = 0.95,
    mapping_per_paper_minimum: float = 0.90,
    performance_sample_question_count: int | None = None,
    performance_warmup_passes: int = 1,
    performance_timed_passes: int = 3,
    p95_latency_ms: float | None = None,
    guardrails_passed: bool = True,
    evidence_mapping_cache: MutableMapping[
        str, EvidenceMappingBundle
    ]
    | None = None,
) -> CandidateRunResult:
    """Run one candidate over the exact required paper/question sets."""

    if not isinstance(embedder, EmbedderAdapter):
        raise StrategyContractError("embedder must implement embed_texts")
    if p95_latency_ms is not None and (
        not math.isfinite(p95_latency_ms) or p95_latency_ms < 0
    ):
        raise StrategyContractError("p95_latency_ms must be finite and non-negative")
    if (
        performance_sample_question_count is not None
        and performance_sample_question_count <= 0
    ):
        raise StrategyContractError(
            "performance_sample_question_count must be greater than zero"
        )
    if performance_warmup_passes <= 0 or performance_timed_passes <= 0:
        raise StrategyContractError(
            "performance warmup/timed passes must be greater than zero"
        )
    normalized_expected_papers = tuple(
        normalize_paper_id(value) for value in expected_paper_ids
    )
    if (
        not normalized_expected_papers
        or len(normalized_expected_papers)
        != len(set(normalized_expected_papers))
    ):
        raise StrategyContractError(
            "expected_paper_ids must be non-empty and unique"
        )
    expected_papers = set(normalized_expected_papers)
    if set(documents) != expected_papers:
        raise StrategyContractError(
            "candidate document set is incomplete: "
            f"missing={sorted(expected_papers - set(documents))}, "
            f"unexpected={sorted(set(documents) - expected_papers)}"
        )
    if (
        not expected_question_ids
        or len(expected_question_ids) != len(set(expected_question_ids))
    ):
        raise StrategyContractError(
            "expected_question_ids must be non-empty and unique"
        )
    expected_questions = set(expected_question_ids)
    row_ids = [_required_text(question, "row_id") for question in questions]
    if len(row_ids) != len(set(row_ids)):
        raise StrategyContractError("question row_ids must be unique")
    if set(row_ids) != expected_questions:
        raise StrategyContractError(
            "candidate question set is incomplete: "
            f"missing={sorted(expected_questions - set(row_ids))}, "
            f"unexpected={sorted(set(row_ids) - expected_questions)}"
        )
    for question in questions:
        if normalize_paper_id(question.get("paper_id")) not in documents:
            raise StrategyContractError(
                f"{question.get('row_id')}: question paper has no document"
            )

    corpus = _prepare_candidate_corpus(candidate, documents, notes)
    mapping_cache_key = canonical_fingerprint(
        {
            "revision": REFERENCE_MATCH_REVISION,
            "fuzzy_threshold": fuzzy_threshold,
            "overall_minimum": mapping_overall_minimum,
            "per_paper_minimum": mapping_per_paper_minimum,
            "questions": questions,
            "pdf_chunks": [
                (chunk.chunk_id, hash_text(chunk.text))
                for chunk in corpus.pdf_chunks
            ],
        }
    )
    mapping = (
        evidence_mapping_cache.get(mapping_cache_key)
        if evidence_mapping_cache is not None
        else None
    )
    if mapping is None:
        mapping = map_all_references(
            questions,
            corpus.pdf_chunks,
            fuzzy_threshold=fuzzy_threshold,
            overall_minimum=mapping_overall_minimum,
            per_paper_minimum=mapping_per_paper_minimum,
        )
        if evidence_mapping_cache is not None:
            evidence_mapping_cache[mapping_cache_key] = mapping
    if not mapping.coverage.passed:
        raise StrategyContractError(
            "evidence mapping gate failed: "
            + "; ".join(mapping.coverage.failures)
        )
    mapping_by_row = {item.row_id: item for item in mapping.mappings}

    pdf_chunk_by_id = {chunk.chunk_id: chunk for chunk in corpus.pdf_chunks}
    pdf_passages = {
        chunk.chunk_id: chunk.text for chunk in corpus.pdf_chunks
    }
    note_passages = {
        chunk.chunk_id: chunk.text for chunk in corpus.note_chunks
    }
    parent_passages = {
        chunk.chunk_id: chunk.text for chunk in corpus.pdf_parents
    }
    pdf_index = _make_search_index(
        pdf_passages,
        retriever=candidate.retriever,
        embedder=embedder,
        embedding_batch_size=embedding_batch_size,
    )
    note_index = _make_search_index(
        note_passages,
        retriever=candidate.retriever,
        embedder=embedder,
        embedding_batch_size=embedding_batch_size,
    )
    parent_index = _make_search_index(
        parent_passages if candidate.requires_parents else {},
        retriever=candidate.retriever,
        embedder=embedder,
        embedding_batch_size=embedding_batch_size,
    )

    needs_query_embeddings = candidate.retriever in {"dense", "hybrid-rrf"}
    query_vectors: Mapping[str, tuple[float, ...]] = {}
    if needs_query_embeddings:
        query_vectors = _embed_mapping(
            embedder,
            {
                _required_text(question, "row_id"): _required_text(
                    question, "question"
                )
                for question in questions
            },
            batch_size=embedding_batch_size,
        )
    elif candidate.retriever != "bm25":
        raise StrategyContractError(
            f"unsupported retriever: {candidate.retriever}"
        )

    if candidate.reranker != "rerank-off":
        if reranker is None:
            raise StrategyContractError(
                f"{candidate.config_id}: reranker adapter is required"
            )
        if (
            reranker.model_id != RERANKER_MODEL_ID
            or reranker.revision != RERANKER_REVISION
        ):
            raise StrategyContractError(
                "reranker model/revision differs from the pinned contract"
            )

    def retrieve(
        question: Mapping[str, Any],
        *,
        measure: bool = False,
    ) -> tuple[tuple[RetrievalHit, ...], float, float]:
        row_id = str(question["row_id"])
        query = _required_text(question, "question")
        query_vector = query_vectors.get(row_id)
        query_started_ns = time.perf_counter_ns() if measure else 0
        pdf_hits = _search(
            pdf_index,
            retriever=candidate.retriever,
            query=query,
            query_embedding=query_vector,
        )
        note_hits = _search(
            note_index,
            retriever=candidate.retriever,
            query=query,
            query_embedding=query_vector,
        )
        parent_hits = _search(
            parent_index,
            retriever=candidate.retriever,
            query=query,
            query_embedding=query_vector,
        )
        hits = _compose_hits(
            candidate,
            pdf_hits=pdf_hits,
            note_hits=note_hits,
            parent_hits=parent_hits,
            note_backlinks=corpus.note_backlinks,
            pdf_chunks=pdf_chunk_by_id,
            top_k=100,
        )
        query_elapsed_ms = (
            (time.perf_counter_ns() - query_started_ns) / 1_000_000.0
            if measure
            else 0.0
        )
        rerank_elapsed_ms = 0.0
        if candidate.reranker != "rerank-off":
            assert reranker is not None and candidate.reranker_depth is not None
            rerank_started_ns = time.perf_counter_ns() if measure else 0
            hits = rerank_hits(
                query,
                hits,
                pdf_passages,
                reranker,
                depth=candidate.reranker_depth,
                keep=candidate.reranker_keep,
                batch_size=reranker_batch_size,
            ).hits
            if measure:
                rerank_elapsed_ms = (
                    time.perf_counter_ns() - rerank_started_ns
                ) / 1_000_000.0
        return tuple(hits), query_elapsed_ms, rerank_elapsed_ms

    sorted_questions = sorted(questions, key=lambda item: str(item["row_id"]))
    question_results: list[QuestionStrategyResult] = []
    for question in sorted_questions:
        row_id = str(question["row_id"])
        hits, _, _ = retrieve(question)
        evidence = mapping_by_row[row_id]
        metrics = score_ranking(
            [hit.item_id for hit in hits],
            evidence.evaluable_groups,
        )
        question_results.append(
            QuestionStrategyResult(
                row_id=row_id,
                paper_id=evidence.paper_id,
                domain=evidence.domain,
                question_type=evidence.question_type,
                ranked_item_ids=tuple(hit.item_id for hit in hits),
                metrics=metrics.metrics,
            )
        )

    latency_metrics: dict[str, object]
    measured_p95_latency_ms: float
    if p95_latency_ms is None:
        performance_questions: list[Mapping[str, Any]] = []
        questions_by_stratum: dict[
            tuple[str, str], list[Mapping[str, Any]]
        ] = defaultdict(list)
        for question in sorted_questions:
            questions_by_stratum[
                (
                    _required_text(question, "domain"),
                    _required_text(question, "question_type"),
                )
            ].append(question)
        for stratum in sorted(questions_by_stratum):
            performance_questions.append(
                min(
                    questions_by_stratum[stratum],
                    key=lambda item: (
                        hash_text(_required_text(item, "row_id")),
                        _required_text(item, "row_id"),
                    ),
                )
            )
        if (
            performance_sample_question_count is not None
            and len(performance_questions)
            != performance_sample_question_count
        ):
            raise StrategyContractError(
                "performance sample count mismatch: "
                f"expected {performance_sample_question_count}, "
                f"found {len(performance_questions)}"
            )

        for _ in range(performance_warmup_passes):
            for question in performance_questions:
                retrieve(question)
        query_latency_samples_ms: list[float] = []
        rerank_latency_samples_ms: list[float] = []
        latency_samples_ms: list[float] = []
        for _ in range(performance_timed_passes):
            for question in performance_questions:
                _, query_ms, rerank_ms = retrieve(question, measure=True)
                query_latency_samples_ms.append(query_ms)
                rerank_latency_samples_ms.append(rerank_ms)
                latency_samples_ms.append(query_ms + rerank_ms)

        def percentile(samples: Sequence[float], probability: float) -> float:
            ordered = sorted(samples)
            if not ordered:
                raise StrategyContractError(
                    "performance sample selection produced no questions"
                )
            position = (len(ordered) - 1) * probability
            lower = math.floor(position)
            upper = math.ceil(position)
            if lower == upper:
                return ordered[lower]
            fraction = position - lower
            return ordered[lower] + (
                ordered[upper] - ordered[lower]
            ) * fraction

        measured_p95_latency_ms = percentile(latency_samples_ms, 0.95)
        latency_metrics = {
            "measurement_revision": "stratified-warm-query-v1",
            "question_selection": (
                "minimum sha256(row_id) per domain x question_type"
            ),
            "query_embedding_mode": "precomputed",
            "warmup_passes": performance_warmup_passes,
            "timed_passes": performance_timed_passes,
            "performance_question_count": len(performance_questions),
            "sample_count": len(latency_samples_ms),
            "query_p50_ms": percentile(query_latency_samples_ms, 0.50),
            "query_p95_ms": percentile(query_latency_samples_ms, 0.95),
            "rerank_p50_ms": percentile(rerank_latency_samples_ms, 0.50),
            "rerank_p95_ms": percentile(rerank_latency_samples_ms, 0.95),
            "p50_latency_ms": percentile(latency_samples_ms, 0.50),
            "p95_latency_ms": measured_p95_latency_ms,
        }
    else:
        measured_p95_latency_ms = float(p95_latency_ms)
        latency_metrics = {
            "measurement_revision": "external-override",
            "p95_latency_ms": measured_p95_latency_ms,
        }

    aggregate = macro_aggregate(
        [
            QuestionScore(
                result.row_id,
                result.paper_id,
                result.domain,
                result.question_type,
                result.metrics,
            )
            for result in question_results
        ]
    )
    indexed_passages = {
        **pdf_passages,
        **note_passages,
        **(
            parent_passages if candidate.requires_parents else {}
        ),
    }
    indexed_vectors = {
        **pdf_index.embeddings,
        **note_index.embeddings,
        **parent_index.embeddings,
    }
    index_bytes = sum(
        len(text.encode("utf-8")) for text in indexed_passages.values()
    ) + sum(4 * len(vector) for vector in indexed_vectors.values())
    return CandidateRunResult(
        candidate=candidate,
        question_results=tuple(question_results),
        aggregate=aggregate,
        mapping=mapping,
        completed_paper_ids=tuple(sorted(documents)),
        completed_question_ids=tuple(sorted(row_ids)),
        p95_latency_ms=measured_p95_latency_ms,
        index_bytes=index_bytes,
        chunk_count=len(indexed_passages),
        guardrails_passed=bool(guardrails_passed),
        latency_metrics=latency_metrics,
    )


@dataclass(frozen=True)
class StageRanking:
    stage_id: str
    ranked: tuple[CandidateRunResult, ...]
    incomplete_config_ids: tuple[str, ...] = ()
    ineligible_config_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "stage_id": self.stage_id,
            "ranked_config_ids": [
                result.candidate.config_id for result in self.ranked
            ],
            "incomplete_config_ids": list(self.incomplete_config_ids),
            "ineligible_config_ids": list(self.ineligible_config_ids),
        }


def rank_stage_results(
    stage_id: str,
    results: Sequence[CandidateRunResult],
    *,
    expected_paper_ids: Sequence[str],
    expected_question_ids: Sequence[str],
    tie_threshold: float = 0.005,
) -> StageRanking:
    """Rank only complete, mapping-passed, guardrail-passed stage candidates."""

    if stage_id not in STAGE_IDS:
        raise StrategyContractError(f"unsupported stage_id: {stage_id}")
    if any(result.candidate.stage_id != stage_id for result in results):
        raise StrategyContractError("stage ranking received a candidate from another stage")
    incomplete = tuple(
        sorted(
            result.candidate.config_id
            for result in results
            if not result.is_complete(
                expected_paper_ids=expected_paper_ids,
                expected_question_ids=expected_question_ids,
            )
        )
    )
    eligible: list[CandidateRunResult] = []
    ineligible: list[str] = []
    for result in results:
        if result.candidate.config_id in incomplete:
            continue
        if (
            not result.candidate.rankable
            or not result.guardrails_passed
            or not result.mapping.coverage.passed
        ):
            ineligible.append(result.candidate.config_id)
            continue
        eligible.append(result)
    evaluable_sets = {
        frozenset(
            (mapping.row_id, group.group_id)
            for mapping in result.mapping.mappings
            for group in mapping.evaluable_groups
        )
        for result in eligible
    }
    if len(evaluable_sets) > 1:
        raise StrategyContractError(
            "stage candidates do not share the same evaluable evidence set"
        )
    summaries = [
        result.summary(
            expected_paper_ids=expected_paper_ids,
            expected_question_ids=expected_question_ids,
        )
        for result in eligible
    ]
    ordered = rank_candidates(summaries, tie_threshold=tie_threshold)
    by_id = {result.candidate.config_id: result for result in eligible}
    return StageRanking(
        stage_id=stage_id,
        ranked=tuple(by_id[summary.config_id] for summary in ordered),
        incomplete_config_ids=incomplete,
        ineligible_config_ids=tuple(sorted(ineligible)),
    )


__all__ = [
    "CandidatePlan",
    "CandidateRunResult",
    "ConfirmationSelection",
    "DEFAULT_FUZZY_THRESHOLD",
    "EmbedderAdapter",
    "EvidenceMappingBundle",
    "QuestionStrategyResult",
    "REFERENCE_MATCH_REVISION",
    "StageRanking",
    "StrategyCandidate",
    "StrategyContractError",
    "UnmappedEvidenceGroup",
    "generate_orthogonal_candidates",
    "load_main_document",
    "load_main_documents",
    "map_all_references",
    "map_question_references",
    "normalize_paper_id",
    "normalize_reference_text",
    "rank_stage_results",
    "run_complete_candidate",
]
