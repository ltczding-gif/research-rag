"""Pure strategy orchestration for the ResearchQA rq-2 live adapter.

This module connects the existing native-source, chunking, retrieval, and
scoring primitives without loading models or touching production state.  Live
callers inject a batch embedder and the already-defined reranker adapter.
"""

from __future__ import annotations

import json
import math
import time
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import (
    Any,
    Callable,
    Mapping,
    MutableMapping,
    Protocol,
    Sequence,
    runtime_checkable,
)
from urllib.parse import urlparse

from benchmarks.researchqa_chunking import (
    EXECUTABLE_PDF_CHUNKER_IDS,
    NOTE_CHUNKER_IDS,
    PDF_CHUNKER_IDS,
    PDF_STRUCTURE_FALLBACK_ID,
    PDF_STRUCTURE_FALLBACK_POLICY,
    NoteChunk,
    ResearchQAChunk,
    chunk_note,
    chunk_pdf,
    note_chunk_pdf_backlinks,
    structure_fallback_corpus_diagnostics,
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
    evidence_group_recall_at_k,
    evaluate_mapping_coverage,
    macro_aggregate,
    map_reference_groups,
    rank_candidates,
    score_ranking,
)
from service.pdf_ir import CanonicalDocument, DocumentPage, hash_text


REFERENCE_MATCH_REVISION = "nfkc-alnum-page-span-v2"
REFERENCE_EXACT_METHOD = "nfkc-alnum-page-span-exact-v2"
REFERENCE_PAGE_HINT_METHOD = "researchqa-page-hint-best-chunk-v2"
REFERENCE_SECTION_HINT_METHOD = "researchqa-section-hint-best-chunk-v2"
DEFAULT_FUZZY_THRESHOLD = 0.86
PAPER_SCOPED_RETRIEVAL = "paper-scoped"
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
    """Apply versioned NFKC + lowercase alphanumeric normalization."""

    if not isinstance(text, str) or not text.strip():
        raise StrategyContractError("reference text must be non-empty")
    normalized = "".join(
        normalized_character
        for character in text
        for normalized_character in unicodedata.normalize(
            "NFKC",
            character,
        ).lower()
        if normalized_character.isalnum()
    )
    if not normalized:
        raise StrategyContractError(
            "reference text must contain an alphanumeric character"
        )
    return normalized


def _normalize_page_with_offsets(text: str) -> tuple[str, tuple[int, ...]]:
    compact: list[str] = []
    offsets: list[int] = []
    for offset, character in enumerate(text):
        for normalized_character in unicodedata.normalize(
            "NFKC",
            character,
        ).lower():
            if normalized_character.isalnum():
                compact.append(normalized_character)
                offsets.append(offset)
    return "".join(compact), tuple(offsets)


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


@dataclass(frozen=True)
class _ReferenceAlignmentContext:
    paper_id: str
    chunks: tuple[ResearchQAChunk, ...]
    compact_chunks: Mapping[str, str]
    chunks_by_page: Mapping[int, tuple[ResearchQAChunk, ...]]
    pages: tuple[tuple[str, tuple[int, ...]], ...]


def _build_reference_alignment_context(
    paper_id: str,
    chunks: Sequence[ResearchQAChunk],
    document: CanonicalDocument | None,
) -> _ReferenceAlignmentContext:
    paper_chunks = tuple(
        sorted(
            (chunk for chunk in chunks if chunk.paper_id == paper_id),
            key=lambda chunk: chunk.chunk_id,
        )
    )
    chunks_by_page: dict[int, list[ResearchQAChunk]] = defaultdict(list)
    for chunk in paper_chunks:
        for page_index in dict.fromkeys(
            span.pdf_page_index for span in chunk.source_spans
        ):
            chunks_by_page[page_index].append(chunk)
    pages: tuple[tuple[str, tuple[int, ...]], ...] = ()
    if document is not None:
        if document.paper_id != paper_id:
            raise StrategyContractError(
                f"document paper_id mismatch: {document.paper_id} != {paper_id}"
            )
        pages = tuple(
            _normalize_page_with_offsets(page.normalized_text)
            for page in document.pages
        )
    return _ReferenceAlignmentContext(
        paper_id=paper_id,
        chunks=paper_chunks,
        compact_chunks={
            chunk.chunk_id: normalize_reference_text(chunk.text)
            for chunk in paper_chunks
        },
        chunks_by_page={
            page_index: tuple(page_chunks)
            for page_index, page_chunks in chunks_by_page.items()
        },
        pages=pages,
    )


def _overlapping_chunk_ids(
    context: _ReferenceAlignmentContext,
    *,
    page_index: int,
    start: int,
    end: int,
) -> tuple[str, ...]:
    matches = []
    for chunk in context.chunks_by_page.get(page_index, ()):
        if any(
            span.pdf_page_index == page_index
            and span.char_start_in_normalized_page < end
            and start < span.char_end_in_normalized_page
            for span in chunk.source_spans
        ):
            matches.append(chunk.chunk_id)
    return tuple(matches)


def _exact_page_span_matches(
    reference: str,
    context: _ReferenceAlignmentContext,
) -> tuple[str, ...]:
    matches: list[str] = []
    for page_index, (page_text, offsets) in enumerate(context.pages):
        position = page_text.find(reference)
        while position >= 0:
            start = offsets[position]
            end = offsets[position + len(reference) - 1] + 1
            matches.extend(
                _overlapping_chunk_ids(
                    context,
                    page_index=page_index,
                    start=start,
                    end=end,
                )
            )
            position = page_text.find(reference, position + 1)
    return tuple(dict.fromkeys(matches))


def _best_chunk_matches(
    reference: str,
    chunks: Sequence[ResearchQAChunk],
    compact_chunks: Mapping[str, str],
) -> tuple[tuple[str, ...], float] | None:
    scores = {
        chunk.chunk_id: _partial_sequence_ratio(
            reference,
            compact_chunks[chunk.chunk_id],
        )
        for chunk in chunks
    }
    if not scores:
        return None
    best = max(scores.values())
    matches = tuple(
        chunk_id
        for chunk_id, score in sorted(scores.items())
        if math.isclose(score, best, rel_tol=0.0, abs_tol=1e-12)
    )
    return matches, best


def map_question_references(
    question: Mapping[str, Any],
    chunks: Sequence[ResearchQAChunk],
    *,
    document: CanonicalDocument | None = None,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
) -> EvidenceMapping:
    """Map one question's AND/OR reference groups to stable chunk IDs."""

    if not 0.0 <= fuzzy_threshold <= 1.0:
        raise StrategyContractError("fuzzy_threshold must be in [0, 1]")
    paper_id = normalize_paper_id(question.get("paper_id"))
    context = _build_reference_alignment_context(
        paper_id,
        chunks,
        document,
    )
    return _map_question_with_context(
        question,
        context,
        fuzzy_threshold=fuzzy_threshold,
    )


def _map_question_with_context(
    question: Mapping[str, Any],
    context: _ReferenceAlignmentContext,
    *,
    fuzzy_threshold: float,
) -> EvidenceMapping:
    row_id = _required_text(question, "row_id")
    paper_id = normalize_paper_id(question.get("paper_id"))
    if context.paper_id != paper_id:
        raise StrategyContractError(
            f"{row_id}: alignment context paper mismatch"
        )
    domain = _required_text(question, "domain")
    question_type = _required_text(question, "question_type")
    references = question.get("expected_references")
    if not isinstance(references, list):
        raise StrategyContractError(
            f"{row_id}: expected_references must be a list"
        )

    section_by_reference = {
        normalize_reference_text(alternative): str(
            group.get("section_label") or ""
        ).strip()
        for group in references
        if isinstance(group, Mapping)
        for alternative in group.get("alternatives", ())
        if isinstance(alternative, str) and alternative.strip()
    }
    page_hint = question.get("metadata_page_hint")
    hinted_page = (
        page_hint - 1
        if isinstance(page_hint, int)
        and not isinstance(page_hint, bool)
        and 1 <= page_hint <= len(context.pages)
        else None
    )
    reference_cache: dict[
        tuple[str, str],
        Mapping[str, object] | None,
    ] = {}

    def mapper(reference: str) -> Mapping[str, object] | None:
        normalized = normalize_reference_text(reference)
        section_label = section_by_reference.get(normalized, "")
        cache_key = (normalized, section_label)
        if cache_key in reference_cache:
            return reference_cache[cache_key]
        exact = _exact_page_span_matches(normalized, context)
        if exact:
            result: Mapping[str, object] | None = {
                "mapped_item_ids": exact,
                "match_method": REFERENCE_EXACT_METHOD,
                "match_score": 1.0,
            }
        else:
            method = REFERENCE_MATCH_REVISION
            candidate_chunks: Sequence[ResearchQAChunk] = context.chunks
            enforce_threshold = True
            if hinted_page is not None:
                candidate_chunks = context.chunks_by_page.get(
                    hinted_page,
                    (),
                )
                method = REFERENCE_PAGE_HINT_METHOD
                enforce_threshold = False
            elif section_label and context.pages:
                normalized_section = normalize_reference_text(section_label)
                section_pages = tuple(
                    page_index
                    for page_index, (page_text, _) in enumerate(context.pages)
                    if normalized_section in page_text
                )
                section_chunk_ids = {
                    chunk.chunk_id
                    for page_index in section_pages
                    for chunk in context.chunks_by_page.get(page_index, ())
                }
                if section_chunk_ids:
                    candidate_chunks = tuple(
                        chunk
                        for chunk in context.chunks
                        if chunk.chunk_id in section_chunk_ids
                    )
                    method = REFERENCE_SECTION_HINT_METHOD
                    enforce_threshold = False
            best_match = _best_chunk_matches(
                normalized,
                candidate_chunks,
                context.compact_chunks,
            )
            if best_match is None:
                result = None
            else:
                matches, best = best_match
                if enforce_threshold and best < fuzzy_threshold:
                    result = None
                else:
                    result = {
                        "mapped_item_ids": matches,
                        "match_method": method,
                        "match_score": best,
                    }
        reference_cache[cache_key] = result
        return result

    return map_reference_groups(
        row_id=row_id,
        paper_id=paper_id,
        domain=domain,
        question_type=question_type,
        reference_groups=references,
        mapper=mapper,
    )


def map_all_references(
    questions: Sequence[Mapping[str, Any]],
    chunks: Sequence[ResearchQAChunk],
    *,
    documents: Mapping[str, CanonicalDocument] | None = None,
    fuzzy_threshold: float = DEFAULT_FUZZY_THRESHOLD,
    overall_minimum: float = 0.95,
    per_paper_minimum: float = 0.90,
) -> EvidenceMappingBundle:
    """Map all questions and retain an explicit unmapped-group ledger."""

    row_ids = [_required_text(question, "row_id") for question in questions]
    if len(row_ids) != len(set(row_ids)):
        raise StrategyContractError("question row_ids must be unique")
    if not 0.0 <= fuzzy_threshold <= 1.0:
        raise StrategyContractError("fuzzy_threshold must be in [0, 1]")
    chunks_by_paper: dict[str, list[ResearchQAChunk]] = defaultdict(list)
    for chunk in chunks:
        chunks_by_paper[chunk.paper_id].append(chunk)
    paper_ids = tuple(
        dict.fromkeys(
            normalize_paper_id(question.get("paper_id"))
            for question in questions
        )
    )
    contexts = {}
    for paper_id in paper_ids:
        document = documents.get(paper_id) if documents is not None else None
        if documents is not None and document is None:
            raise StrategyContractError(
                f"question paper has no alignment document: {paper_id}"
            )
        contexts[paper_id] = _build_reference_alignment_context(
            paper_id,
            chunks_by_paper.get(paper_id, ()),
            document,
        )
    mappings = tuple(
        _map_question_with_context(
            question,
            contexts[normalize_paper_id(question.get("paper_id"))],
            fuzzy_threshold=fuzzy_threshold,
        )
        for question in questions
    )
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
        _require_member(
            self.pdf_chunker,
            EXECUTABLE_PDF_CHUNKER_IDS,
            "pdf_chunker",
        )
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
    note_rows = _option_rows(stages, "note_chunkers")
    note_ids = tuple(str(row["id"]) for row in note_rows)
    if set(note_ids) != set(NOTE_CHUNKER_IDS):
        raise StrategyContractError(
            f"config.stages.note_chunkers must be exactly {list(NOTE_CHUNKER_IDS)}"
        )
    note_options = {str(row["id"]): row for row in note_rows}
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
            rankable=bool(note_options[note_id].get("rankable", True)),
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
    confirmation_ids: set[str] = set()
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
                        candidate = _candidate(
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
                        if candidate.config_id not in confirmation_ids:
                            confirmation_ids.add(candidate.config_id)
                            confirmations.append(candidate)
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


def generate_f2_candidate(
    config: Mapping[str, Any],
) -> StrategyCandidate:
    """Create the independent F2 repair candidate without changing the 35."""

    stages = config.get("stages")
    if not isinstance(stages, Mapping):
        raise StrategyContractError("config.stages must be a mapping")
    reranker_rows = _option_rows(stages, "rerankers")
    reranker_options = {str(row["id"]): row for row in reranker_rows}
    if set(reranker_options) != set(RERANKER_IDS):
        raise StrategyContractError(
            f"rerankers must be exactly {list(RERANKER_IDS)}"
        )
    components = {
        "stage_id": "pdf-chunker",
        "pdf_chunker": PDF_STRUCTURE_FALLBACK_ID,
        "note_chunker": None,
        "retriever": "dense",
        "source_composition": "pdf-only",
        "reranker": "rerank-off",
    }
    identity = {
        "repair_id": "F2",
        "components": components,
        "policy": dict(PDF_STRUCTURE_FALLBACK_POLICY),
    }
    reranker_option = reranker_options["rerank-off"]
    return StrategyCandidate(
        config_id=f"repair-f2-{canonical_fingerprint(identity)[:20]}",
        reranker_depth=None,
        reranker_keep=int(reranker_option.get("output_k", 10)),
        **components,
    )


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
    ranked_scores: tuple[float, ...] = ()
    pre_rerank_item_ids: tuple[str, ...] = ()
    pre_rerank_scores: tuple[float, ...] = ()
    pre_rerank_metrics: Mapping[str, float | None] = field(
        default_factory=dict
    )

    def to_dict(self) -> dict[str, object]:
        return {
            "row_id": self.row_id,
            "paper_id": self.paper_id,
            "domain": self.domain,
            "question_type": self.question_type,
            "ranked_item_ids": list(self.ranked_item_ids),
            "ranked_scores": list(self.ranked_scores),
            "pre_rerank_item_ids": list(self.pre_rerank_item_ids),
            "pre_rerank_scores": list(self.pre_rerank_scores),
            "pre_rerank_metrics": dict(self.pre_rerank_metrics),
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
    retrieval_scope: str = PAPER_SCOPED_RETRIEVAL
    latency_metrics: Mapping[str, object] = field(default_factory=dict)
    corpus_diagnostics: Mapping[str, object] = field(default_factory=dict)

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
            latency_decisive=(
                self.latency_metrics.get("validity") == "decisive"
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
            "retrieval_scope": self.retrieval_scope,
            "latency_metrics": dict(self.latency_metrics),
            "corpus_diagnostics": dict(self.corpus_diagnostics),
        }


@dataclass(frozen=True)
class _CandidateCorpus:
    pdf_chunks: tuple[ResearchQAChunk, ...]
    pdf_parents: tuple[ResearchQAChunk, ...]
    note_chunks: tuple[NoteChunk, ...]
    note_backlinks: Mapping[str, tuple[str, ...]]
    diagnostics: Mapping[str, object] = field(default_factory=dict)


def _prepare_candidate_corpus(
    candidate: StrategyCandidate,
    documents: Mapping[str, CanonicalDocument],
    notes: Mapping[str, str] | None,
) -> _CandidateCorpus:
    pdf_chunks: list[ResearchQAChunk] = []
    pdf_parents: list[ResearchQAChunk] = []
    pdf_chunking_results = {}
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
        if candidate.pdf_chunker == PDF_STRUCTURE_FALLBACK_ID:
            pdf_chunking_results[paper_id] = result
    corpus_diagnostics: dict[str, object] = {}
    if candidate.pdf_chunker == PDF_STRUCTURE_FALLBACK_ID:
        f2_diagnostics = structure_fallback_corpus_diagnostics(
            pdf_chunking_results
        )
        corpus_diagnostics["pdf_chunking"] = f2_diagnostics
        if f2_diagnostics.get("contract_status") != "passed":
            raise StrategyContractError(
                f"{candidate.config_id}: F2 global output cost "
                f"{f2_diagnostics['output_to_fixed_1200_ratio']:.6f} "
                "exceeds 1.250000"
            )
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
            if result.status != "completed":
                raise StrategyContractError(
                    f"{paper_id}/{candidate.note_chunker}: "
                    f"{result.failure_reason or 'no note chunks'}"
                )
            if (
                not result.chunks
                and candidate.note_chunker != "note-reviewer-concern"
            ):
                raise StrategyContractError(
                    f"{paper_id}/{candidate.note_chunker}: no note chunks"
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
        diagnostics=corpus_diagnostics,
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


CandidateProgressCallback = Callable[[Mapping[str, object]], None]


def _question_result_from_progress(
    value: Mapping[str, Any],
) -> QuestionStrategyResult:
    def text(key: str) -> str:
        result = value.get(key)
        if not isinstance(result, str) or not result:
            raise StrategyContractError(
                f"resume question result has invalid {key}"
            )
        return result

    def texts(key: str) -> tuple[str, ...]:
        result = value.get(key)
        if not isinstance(result, list) or any(
            not isinstance(item, str) or not item for item in result
        ):
            raise StrategyContractError(
                f"resume question result has invalid {key}"
            )
        return tuple(result)

    def scores(key: str) -> tuple[float, ...]:
        result = value.get(key)
        if not isinstance(result, list):
            raise StrategyContractError(
                f"resume question result has invalid {key}"
            )
        converted = tuple(float(item) for item in result)
        if any(not math.isfinite(item) for item in converted):
            raise StrategyContractError(
                f"resume question result has non-finite {key}"
            )
        return converted

    def metrics(key: str) -> dict[str, float | None]:
        result = value.get(key)
        if not isinstance(result, Mapping):
            raise StrategyContractError(
                f"resume question result has invalid {key}"
            )
        converted: dict[str, float | None] = {}
        for name, item in result.items():
            if not isinstance(name, str) or not name:
                raise StrategyContractError(
                    f"resume question result has invalid {key} name"
                )
            if item is None:
                converted[name] = None
                continue
            number = float(item)
            if not math.isfinite(number):
                raise StrategyContractError(
                    f"resume question result has non-finite {key}"
                )
            converted[name] = number
        return converted

    ranked_item_ids = texts("ranked_item_ids")
    ranked_scores = scores("ranked_scores")
    pre_rerank_item_ids = texts("pre_rerank_item_ids")
    pre_rerank_scores = scores("pre_rerank_scores")
    if len(ranked_item_ids) != len(ranked_scores) or len(
        pre_rerank_item_ids
    ) != len(pre_rerank_scores):
        raise StrategyContractError(
            "resume question result item/score lengths differ"
        )
    return QuestionStrategyResult(
        row_id=text("row_id"),
        paper_id=normalize_paper_id(text("paper_id")),
        domain=text("domain"),
        question_type=text("question_type"),
        ranked_item_ids=ranked_item_ids,
        ranked_scores=ranked_scores,
        pre_rerank_item_ids=pre_rerank_item_ids,
        pre_rerank_scores=pre_rerank_scores,
        pre_rerank_metrics=metrics("pre_rerank_metrics"),
        metrics=metrics("metrics"),
    )


def _annotate_candidate_failure(
    exc: Exception,
    *,
    phase: str,
    paper_id: str | None,
    row_id: str | None,
    pass_kind: str | None = None,
    pass_index: int | None = None,
) -> None:
    exc.researchqa_failure_context = {
        "phase": phase,
        "paper_id": paper_id,
        "row_id": row_id,
        "pass_kind": pass_kind,
        "pass_index": pass_index,
    }


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
    reranker_batch_size: int = 1,
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
    resume_progress: Mapping[str, Any] | None = None,
    progress_callback: CandidateProgressCallback | None = None,
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
    if reranker_batch_size <= 0:
        raise StrategyContractError(
            "reranker_batch_size must be greater than zero"
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

    sorted_questions = sorted(questions, key=lambda item: str(item["row_id"]))
    question_by_row = {
        _required_text(question, "row_id"): question
        for question in sorted_questions
    }
    questions_by_paper: dict[str, list[Mapping[str, Any]]] = {
        paper_id: [] for paper_id in normalized_expected_papers
    }
    for question in sorted_questions:
        questions_by_paper[
            normalize_paper_id(question.get("paper_id"))
        ].append(question)

    completed_papers: set[str] = set()
    question_results_by_id: dict[str, QuestionStrategyResult] = {}
    resume_warmup_completed = 0
    resume_timed_passes: list[dict[str, object]] = []
    resume_performance_question_ids: tuple[str, ...] = ()
    if resume_progress is not None:
        if (
            resume_progress.get("progress_schema_version") != 1
            or resume_progress.get("phase")
            not in {
                "preparing",
                "quality",
                "latency-warmup",
                "latency-timed",
                "aggregate",
            }
        ):
            raise StrategyContractError(
                "candidate resume progress schema/phase is invalid"
            )
        raw_papers = resume_progress.get("completed_paper_ids")
        raw_question_ids = resume_progress.get("completed_question_ids")
        raw_results = resume_progress.get("question_results")
        if (
            not isinstance(raw_papers, list)
            or not isinstance(raw_question_ids, list)
            or not isinstance(raw_results, list)
            or any(not isinstance(item, str) for item in raw_papers)
            or any(not isinstance(item, str) for item in raw_question_ids)
            or any(not isinstance(item, Mapping) for item in raw_results)
        ):
            raise StrategyContractError(
                "candidate resume quality progress is invalid"
            )
        completed_papers = {
            normalize_paper_id(item) for item in raw_papers
        }
        if (
            len(completed_papers) != len(raw_papers)
            or not completed_papers <= expected_papers
            or len(set(raw_question_ids)) != len(raw_question_ids)
            or not set(raw_question_ids) <= expected_questions
        ):
            raise StrategyContractError(
                "candidate resume completed ID sets are invalid"
            )
        for raw_result in raw_results:
            result = _question_result_from_progress(raw_result)
            if result.row_id in question_results_by_id:
                raise StrategyContractError(
                    "candidate resume question results contain duplicates"
                )
            question = question_by_row.get(result.row_id)
            if (
                question is None
                or result.paper_id
                != normalize_paper_id(question.get("paper_id"))
                or result.domain != _required_text(question, "domain")
                or result.question_type
                != _required_text(question, "question_type")
            ):
                raise StrategyContractError(
                    f"candidate resume row identity differs: {result.row_id}"
                )
            question_results_by_id[result.row_id] = result
        expected_completed_rows = {
            _required_text(question, "row_id")
            for paper_id in completed_papers
            for question in questions_by_paper[paper_id]
        }
        if (
            set(raw_question_ids) != set(question_results_by_id)
            or set(question_results_by_id) != expected_completed_rows
        ):
            raise StrategyContractError(
                "candidate resume contains a partial paper boundary"
            )
        raw_performance_ids = resume_progress.get(
            "performance_question_ids",
            [],
        )
        raw_warmup = resume_progress.get("warmup_completed_passes", 0)
        raw_timed = resume_progress.get("timed_passes", [])
        if (
            not isinstance(raw_performance_ids, list)
            or any(
                not isinstance(item, str) or not item
                for item in raw_performance_ids
            )
            or not isinstance(raw_warmup, int)
            or isinstance(raw_warmup, bool)
            or raw_warmup < 0
            or not isinstance(raw_timed, list)
            or any(not isinstance(item, Mapping) for item in raw_timed)
        ):
            raise StrategyContractError(
                "candidate resume latency progress is invalid"
            )
        resume_performance_question_ids = tuple(raw_performance_ids)
        resume_warmup_completed = raw_warmup
        resume_timed_passes = [dict(item) for item in raw_timed]

    performance_question_ids: tuple[str, ...] = ()
    warmup_completed_passes = resume_warmup_completed
    timed_passes = list(resume_timed_passes)

    def emit_progress(phase: str) -> None:
        if progress_callback is None:
            return
        ordered_results = [
            question_results_by_id[row_id].to_dict()
            for row_id in sorted(question_results_by_id)
        ]
        progress_callback(
            {
                "progress_schema_version": 1,
                "phase": phase,
                "completed_paper_ids": sorted(completed_papers),
                "completed_question_ids": [
                    str(result["row_id"]) for result in ordered_results
                ],
                "question_results": ordered_results,
                "performance_question_ids": list(
                    performance_question_ids
                ),
                "warmup_completed_passes": warmup_completed_passes,
                "timed_passes": [dict(item) for item in timed_passes],
            }
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
            documents=documents,
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

    def passages_by_paper(
        chunks: Sequence[ResearchQAChunk | NoteChunk],
    ) -> dict[str, dict[str, str]]:
        grouped = {paper_id: {} for paper_id in sorted(documents)}
        for chunk in chunks:
            grouped[chunk.paper_id][chunk.chunk_id] = chunk.text
        return grouped

    pdf_passages_by_paper = passages_by_paper(corpus.pdf_chunks)
    note_passages_by_paper = passages_by_paper(corpus.note_chunks)
    parent_passages_by_paper = passages_by_paper(corpus.pdf_parents)
    pdf_indexes = {
        paper_id: _make_search_index(
            passages,
            retriever=candidate.retriever,
            embedder=embedder,
            embedding_batch_size=embedding_batch_size,
        )
        for paper_id, passages in pdf_passages_by_paper.items()
    }
    note_indexes = {
        paper_id: _make_search_index(
            passages,
            retriever=candidate.retriever,
            embedder=embedder,
            embedding_batch_size=embedding_batch_size,
        )
        for paper_id, passages in note_passages_by_paper.items()
    }
    parent_indexes = {
        paper_id: _make_search_index(
            passages if candidate.requires_parents else {},
            retriever=candidate.retriever,
            embedder=embedder,
            embedding_batch_size=embedding_batch_size,
        )
        for paper_id, passages in parent_passages_by_paper.items()
    }

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
    ) -> tuple[
        tuple[RetrievalHit, ...],
        float,
        float,
        tuple[RetrievalHit, ...],
    ]:
        row_id = str(question["row_id"])
        paper_id = normalize_paper_id(question.get("paper_id"))
        query = _required_text(question, "question")
        query_vector = query_vectors.get(row_id)
        query_started_ns = time.perf_counter_ns() if measure else 0
        pdf_hits = _search(
            pdf_indexes[paper_id],
            retriever=candidate.retriever,
            query=query,
            query_embedding=query_vector,
        )
        note_hits = _search(
            note_indexes[paper_id],
            retriever=candidate.retriever,
            query=query,
            query_embedding=query_vector,
        )
        parent_hits = _search(
            parent_indexes[paper_id],
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
        pre_rerank_hits = tuple(hits)
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
        return (
            tuple(hits),
            query_elapsed_ms,
            rerank_elapsed_ms,
            pre_rerank_hits,
        )

    for paper_id in normalized_expected_papers:
        if paper_id in completed_papers:
            continue
        paper_results: list[QuestionStrategyResult] = []
        for question in questions_by_paper[paper_id]:
            row_id = str(question["row_id"])
            try:
                hits, _, _, pre_rerank_hits = retrieve(question)
            except Exception as exc:
                _annotate_candidate_failure(
                    exc,
                    phase="quality",
                    paper_id=paper_id,
                    row_id=row_id,
                )
                raise
            evidence = mapping_by_row[row_id]
            metrics = score_ranking(
                [hit.item_id for hit in hits],
                evidence.evaluable_groups,
            )
            if candidate.reranker != "rerank-off":
                pre_rerank_ids = [
                    hit.item_id for hit in pre_rerank_hits
                ]
                pre_rerank_metrics = {
                    **score_ranking(
                        pre_rerank_ids,
                        evidence.evaluable_groups,
                    ).metrics,
                    **{
                        f"recall_at_{cutoff}": evidence_group_recall_at_k(
                            pre_rerank_ids,
                            evidence.evaluable_groups,
                            cutoff,
                        )
                        for cutoff in (20, 50, 100)
                    },
                }
            else:
                pre_rerank_metrics = {}
            paper_results.append(
                QuestionStrategyResult(
                    row_id=row_id,
                    paper_id=evidence.paper_id,
                    domain=evidence.domain,
                    question_type=evidence.question_type,
                    ranked_item_ids=tuple(hit.item_id for hit in hits),
                    ranked_scores=tuple(float(hit.score) for hit in hits),
                    pre_rerank_item_ids=(
                        tuple(hit.item_id for hit in pre_rerank_hits)
                        if candidate.reranker != "rerank-off"
                        else ()
                    ),
                    pre_rerank_scores=(
                        tuple(float(hit.score) for hit in pre_rerank_hits)
                        if candidate.reranker != "rerank-off"
                        else ()
                    ),
                    pre_rerank_metrics=pre_rerank_metrics,
                    metrics=metrics.metrics,
                )
            )
        question_results_by_id.update(
            {result.row_id: result for result in paper_results}
        )
        completed_papers.add(paper_id)
        emit_progress("quality")

    question_results = [
        question_results_by_id[row_id]
        for row_id in sorted(question_results_by_id)
    ]
    if (
        set(question_results_by_id) != expected_questions
        or completed_papers != expected_papers
    ):
        raise StrategyContractError(
            "candidate quality progress did not complete the expected set"
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

        performance_question_ids = tuple(
            _required_text(question, "row_id")
            for question in performance_questions
        )
        if resume_performance_question_ids and (
            resume_performance_question_ids != performance_question_ids
        ):
            raise StrategyContractError(
                "candidate resume performance question set differs"
            )
        if (
            not resume_performance_question_ids
            and (resume_warmup_completed or resume_timed_passes)
        ):
            raise StrategyContractError(
                "candidate resume latency samples lack question identity"
            )
        if warmup_completed_passes > performance_warmup_passes:
            raise StrategyContractError(
                "candidate resume has too many warmup passes"
            )
        if len(timed_passes) > performance_timed_passes:
            raise StrategyContractError(
                "candidate resume has too many timed passes"
            )

        def pass_samples(
            value: Mapping[str, object],
            key: str,
        ) -> list[float]:
            raw = value.get(key)
            if not isinstance(raw, list):
                raise StrategyContractError(
                    f"candidate resume timed pass has invalid {key}"
                )
            converted = [float(item) for item in raw]
            if (
                len(converted) != len(performance_questions)
                or any(
                    not math.isfinite(item) or item < 0
                    for item in converted
                )
            ):
                raise StrategyContractError(
                    f"candidate resume timed pass has invalid {key}"
                )
            return converted

        normalized_timed_passes: list[dict[str, object]] = []
        for expected_index, value in enumerate(timed_passes):
            if (
                value.get("pass_index") != expected_index
                or value.get("question_ids")
                != list(performance_question_ids)
            ):
                raise StrategyContractError(
                    "candidate resume timed pass identity differs"
                )
            query_samples = pass_samples(value, "query_samples_ms")
            rerank_samples = pass_samples(value, "rerank_samples_ms")
            total_samples = pass_samples(value, "latency_samples_ms")
            if any(
                not math.isclose(
                    total,
                    query + rerank,
                    rel_tol=1e-9,
                    abs_tol=1e-9,
                )
                for total, query, rerank in zip(
                    total_samples,
                    query_samples,
                    rerank_samples,
                    strict=True,
                )
            ):
                raise StrategyContractError(
                    "candidate resume timed pass totals differ"
                )
            normalized_timed_passes.append(
                {
                    "pass_index": expected_index,
                    "question_ids": list(performance_question_ids),
                    "query_samples_ms": query_samples,
                    "rerank_samples_ms": rerank_samples,
                    "latency_samples_ms": total_samples,
                }
            )
        timed_passes = normalized_timed_passes

        for pass_index in range(
            warmup_completed_passes,
            performance_warmup_passes,
        ):
            for question in performance_questions:
                row_id = _required_text(question, "row_id")
                paper_id = normalize_paper_id(question.get("paper_id"))
                try:
                    retrieve(question)
                except Exception as exc:
                    _annotate_candidate_failure(
                        exc,
                        phase="latency",
                        paper_id=paper_id,
                        row_id=row_id,
                        pass_kind="warmup",
                        pass_index=pass_index,
                    )
                    raise
            warmup_completed_passes = pass_index + 1
            emit_progress("latency-warmup")

        for pass_index in range(
            len(timed_passes),
            performance_timed_passes,
        ):
            query_pass_samples: list[float] = []
            rerank_pass_samples: list[float] = []
            latency_pass_samples: list[float] = []
            for question in performance_questions:
                row_id = _required_text(question, "row_id")
                paper_id = normalize_paper_id(question.get("paper_id"))
                try:
                    _, query_ms, rerank_ms, _ = retrieve(
                        question,
                        measure=True,
                    )
                except Exception as exc:
                    _annotate_candidate_failure(
                        exc,
                        phase="latency",
                        paper_id=paper_id,
                        row_id=row_id,
                        pass_kind="timed",
                        pass_index=pass_index,
                    )
                    raise
                query_pass_samples.append(query_ms)
                rerank_pass_samples.append(rerank_ms)
                latency_pass_samples.append(query_ms + rerank_ms)
            timed_passes.append(
                {
                    "pass_index": pass_index,
                    "question_ids": list(performance_question_ids),
                    "query_samples_ms": query_pass_samples,
                    "rerank_samples_ms": rerank_pass_samples,
                    "latency_samples_ms": latency_pass_samples,
                }
            )
            emit_progress("latency-timed")

        query_latency_samples_ms = [
            float(sample)
            for item in timed_passes
            for sample in item["query_samples_ms"]
        ]
        rerank_latency_samples_ms = [
            float(sample)
            for item in timed_passes
            for sample in item["rerank_samples_ms"]
        ]
        latency_samples_ms = [
            float(sample)
            for item in timed_passes
            for sample in item["latency_samples_ms"]
        ]

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
            "measurement_revision": "stratified-warm-query-v2",
            "question_selection": (
                "minimum sha256(row_id) per domain x question_type"
            ),
            "query_embedding_mode": "precomputed",
            "reranker_batch_size": reranker_batch_size,
            "warmup_passes": performance_warmup_passes,
            "timed_passes": performance_timed_passes,
            "performance_question_count": len(performance_questions),
            "performance_question_ids": list(performance_question_ids),
            "sample_count": len(latency_samples_ms),
            "pass_samples": [dict(item) for item in timed_passes],
            "validity": "observed-only",
            "validity_reason": "serial-uncontrolled-environment",
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
            "validity": "observed-only",
            "validity_reason": "external-override",
        }

    emit_progress("aggregate")
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
        **{
            item_id: vector
            for index in pdf_indexes.values()
            for item_id, vector in index.embeddings.items()
        },
        **{
            item_id: vector
            for index in note_indexes.values()
            for item_id, vector in index.embeddings.items()
        },
        **{
            item_id: vector
            for index in parent_indexes.values()
            for item_id, vector in index.embeddings.items()
        },
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
        corpus_diagnostics=corpus.diagnostics,
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
    "PAPER_SCOPED_RETRIEVAL",
    "QuestionStrategyResult",
    "REFERENCE_MATCH_REVISION",
    "StageRanking",
    "StrategyCandidate",
    "StrategyContractError",
    "UnmappedEvidenceGroup",
    "generate_orthogonal_candidates",
    "generate_f2_candidate",
    "load_main_document",
    "load_main_documents",
    "map_all_references",
    "map_question_references",
    "normalize_paper_id",
    "normalize_reference_text",
    "rank_stage_results",
    "run_complete_candidate",
]
