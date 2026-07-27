"""Canonical page/span PDF representation and legacy C0 adapter.

The module is intentionally side-effect free at import time. It defines the
Wave 1A intermediate representation, a page-preserving pdfplumber extractor,
and an adapter that reproduces the legacy fixed-character windows while
attaching stable source provenance.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import pdfplumber

try:
    from .pdf_baseline import (
        DEFAULT_CHUNK_SIZE,
        DEFAULT_CHUNK_STEP,
        DEFAULT_MIN_CHUNK_LEN,
        truncate_final_references,
    )
except ImportError:  # Support the existing flat service/ import convention.
    from pdf_baseline import (
        DEFAULT_CHUNK_SIZE,
        DEFAULT_CHUNK_STEP,
        DEFAULT_MIN_CHUNK_LEN,
        truncate_final_references,
    )


CHUNK_SCHEMA_VERSION = 1
_SHA256_LENGTH = 64
_EXTRACTOR_CONFIG = {
    "extractor": "pdfplumber-page-text",
    "normalization": "lf-newlines",
    "layout_structure": "unclassified",
    "schema_version": CHUNK_SCHEMA_VERSION,
}


class CanonicalIRError(ValueError):
    """Raised when canonical provenance cannot be constructed safely."""


class EvidenceMappingError(CanonicalIRError):
    """Raised when a stable evidence span cannot map to candidate chunks."""


class ProvenanceMismatchError(EvidenceMappingError):
    """Raised when an evidence locator targets a different canonical page."""


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def hash_text(text: str) -> str:
    """Return the canonical UTF-8 SHA-256 for one text value."""
    return _sha256_bytes(text.encode("utf-8"))


def _fingerprint(value: Mapping[str, object]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hash_text(payload)


DEFAULT_EXTRACTOR_FINGERPRINT = _fingerprint(_EXTRACTOR_CONFIG)


def normalize_page_text(text: str) -> str:
    """Apply the minimal deterministic normalization used by page hashes."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def hash_file(path: str | Path, *, block_size: int = 1024 * 1024) -> str:
    """Stream one file into a SHA-256 digest."""
    if block_size <= 0:
        raise ValueError("block_size must be greater than zero")
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(block_size), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_identifier(value: str, field_name: str) -> None:
    if not value or value.strip() != value:
        raise CanonicalIRError(f"{field_name} must be a non-empty trimmed string")


def _require_sha256(value: str, field_name: str) -> None:
    if (
        len(value) != _SHA256_LENGTH
        or value.lower() != value
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise CanonicalIRError(f"{field_name} must be a lowercase SHA-256")


@dataclass(frozen=True)
class DocumentPage:
    """One normalized physical PDF page with a stable content hash."""

    paper_id: str
    file_id: str
    pdf_page_index: int
    printed_page_label: str | None
    normalized_text: str
    page_text_hash: str
    extraction_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.paper_id, "paper_id")
        _require_identifier(self.file_id, "file_id")
        if self.pdf_page_index < 0:
            raise CanonicalIRError("pdf_page_index must be non-negative")
        if (
            self.printed_page_label is not None
            and not self.printed_page_label.strip()
        ):
            raise CanonicalIRError(
                "printed_page_label must be non-blank when provided"
            )
        _require_sha256(self.page_text_hash, "page_text_hash")
        if hash_text(self.normalized_text) != self.page_text_hash:
            raise CanonicalIRError(
                "page_text_hash does not match normalized_text"
            )

    @classmethod
    def create(
        cls,
        *,
        paper_id: str,
        file_id: str,
        pdf_page_index: int,
        text: str,
        printed_page_label: str | None = None,
        extraction_warnings: Sequence[str] = (),
    ) -> "DocumentPage":
        normalized = normalize_page_text(text)
        return cls(
            paper_id=paper_id,
            file_id=file_id,
            pdf_page_index=pdf_page_index,
            printed_page_label=printed_page_label,
            normalized_text=normalized,
            page_text_hash=hash_text(normalized),
            extraction_warnings=tuple(extraction_warnings),
        )


@dataclass(frozen=True)
class CanonicalDocument:
    """All page units and immutable extraction provenance for one PDF."""

    paper_id: str
    file_id: str
    file_hash: str
    extractor_fingerprint: str
    pages: tuple[DocumentPage, ...]
    extraction_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_identifier(self.paper_id, "paper_id")
        _require_identifier(self.file_id, "file_id")
        _require_sha256(self.file_hash, "file_hash")
        _require_sha256(self.extractor_fingerprint, "extractor_fingerprint")
        if not self.pages:
            raise CanonicalIRError("canonical document must contain pages")
        expected_indexes = list(range(len(self.pages)))
        actual_indexes = [page.pdf_page_index for page in self.pages]
        if actual_indexes != expected_indexes:
            raise CanonicalIRError(
                "document pages must be contiguous and zero-indexed"
            )
        for page in self.pages:
            if page.paper_id != self.paper_id or page.file_id != self.file_id:
                raise CanonicalIRError(
                    "document page identity does not match its document"
                )


@dataclass(frozen=True)
class SourceSpan:
    """Half-open character locator inside one normalized physical page."""

    file_id: str
    pdf_page_index: int
    char_start_in_normalized_page: int
    char_end_in_normalized_page: int
    page_text_hash: str

    def __post_init__(self) -> None:
        _require_identifier(self.file_id, "file_id")
        if self.pdf_page_index < 0:
            raise CanonicalIRError("pdf_page_index must be non-negative")
        if self.char_start_in_normalized_page < 0:
            raise CanonicalIRError("source span start must be non-negative")
        if (
            self.char_end_in_normalized_page
            <= self.char_start_in_normalized_page
        ):
            raise CanonicalIRError("source span must be non-empty")
        _require_sha256(self.page_text_hash, "page_text_hash")

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ChunkRecord:
    """One indexable C0 chunk with explicit page/span provenance."""

    schema_version: int
    chunk_id: str
    paper_id: str
    file_id: str
    file_hash: str
    is_main: bool
    is_si: bool
    start_page: int
    end_page: int
    section_path: tuple[str, ...]
    source_spans: tuple[SourceSpan, ...]
    text: str
    text_hash: str
    extractor_fingerprint: str
    chunker_fingerprint: str
    previous_chunk_id: str | None
    next_chunk_id: str | None
    extraction_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version != CHUNK_SCHEMA_VERSION:
            raise CanonicalIRError(
                f"unsupported chunk schema version: {self.schema_version}"
            )
        _require_identifier(self.chunk_id, "chunk_id")
        _require_identifier(self.paper_id, "paper_id")
        _require_identifier(self.file_id, "file_id")
        _require_sha256(self.file_hash, "file_hash")
        _require_sha256(self.text_hash, "text_hash")
        _require_sha256(self.extractor_fingerprint, "extractor_fingerprint")
        _require_sha256(self.chunker_fingerprint, "chunker_fingerprint")
        if not isinstance(self.is_main, bool) or not isinstance(self.is_si, bool):
            raise CanonicalIRError("is_main and is_si must be bool values")
        if self.is_main == self.is_si:
            raise CanonicalIRError(
                "exactly one of is_main and is_si must be true"
            )
        if self.start_page < 0 or self.end_page < self.start_page:
            raise CanonicalIRError("invalid chunk page range")
        if not self.source_spans:
            raise CanonicalIRError("indexable chunk must contain source spans")
        if hash_text(self.text) != self.text_hash:
            raise CanonicalIRError("text_hash does not match chunk text")
        if self.start_page != self.source_spans[0].pdf_page_index:
            raise CanonicalIRError("start_page does not match source spans")
        if self.end_page != self.source_spans[-1].pdf_page_index:
            raise CanonicalIRError("end_page does not match source spans")
        if any(span.file_id != self.file_id for span in self.source_spans):
            raise CanonicalIRError("chunk source spans mix file identities")
        if any(
            left.pdf_page_index > right.pdf_page_index
            for left, right in zip(self.source_spans, self.source_spans[1:])
        ):
            raise CanonicalIRError("chunk source spans are not page ordered")

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["section_path"] = list(self.section_path)
        value["source_spans"] = [span.to_dict() for span in self.source_spans]
        value["extraction_warnings"] = list(self.extraction_warnings)
        return value


def extract_pdf_document(
    pdf_path: str | Path,
    *,
    paper_id: str,
    file_id: str,
    expected_file_hash: str | None = None,
    printed_page_labels: Mapping[int, str] | None = None,
) -> CanonicalDocument:
    """Extract one PDF into canonical page units without inferring structure."""
    path = Path(pdf_path)
    actual_file_hash = hash_file(path)
    if expected_file_hash is not None:
        _require_sha256(expected_file_hash, "expected_file_hash")
        if actual_file_hash != expected_file_hash:
            raise CanonicalIRError(
                f"file hash mismatch for {file_id}: "
                f"expected {expected_file_hash}, found {actual_file_hash}"
            )

    pages = []
    with pdfplumber.open(path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            raw_text = page.extract_text()
            warnings = []
            if raw_text is None or not raw_text:
                warnings.append("empty-page-text")
            pages.append(
                DocumentPage.create(
                    paper_id=paper_id,
                    file_id=file_id,
                    pdf_page_index=page_index,
                    text=raw_text or "",
                    printed_page_label=(
                        printed_page_labels.get(page_index)
                        if printed_page_labels is not None
                        else None
                    ),
                    extraction_warnings=warnings,
                )
            )

    return CanonicalDocument(
        paper_id=paper_id,
        file_id=file_id,
        file_hash=actual_file_hash,
        extractor_fingerprint=DEFAULT_EXTRACTOR_FINGERPRINT,
        pages=tuple(pages),
        extraction_warnings=("layout-structure-unclassified",),
    )


def legacy_c0_chunker_fingerprint(
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_step: int = DEFAULT_CHUNK_STEP,
    min_chunk_len: int = DEFAULT_MIN_CHUNK_LEN,
) -> str:
    """Fingerprint one fixed-character C0 configuration."""
    _validate_chunk_config(chunk_size, chunk_step, min_chunk_len)
    return _fingerprint(
        {
            "chunker": "legacy-fixed-character",
            "chunk_size": chunk_size,
            "chunk_step": chunk_step,
            "min_chunk_len_strictly_greater_than": min_chunk_len,
            "reference_policy": "truncate-at-final-reference-like-heading",
            "schema_version": CHUNK_SCHEMA_VERSION,
        }
    )


def _validate_chunk_config(
    chunk_size: int,
    chunk_step: int,
    min_chunk_len: int,
) -> None:
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if chunk_step <= 0:
        raise ValueError("chunk_step must be greater than zero")
    if min_chunk_len < 0:
        raise ValueError("min_chunk_len must be non-negative")


def _joined_page_ranges(
    pages: Sequence[DocumentPage],
) -> tuple[str, tuple[tuple[DocumentPage, int, int], ...]]:
    text_parts = []
    ranges = []
    cursor = 0
    for index, page in enumerate(pages):
        if index:
            text_parts.append("\n")
            cursor += 1
        start = cursor
        text_parts.append(page.normalized_text)
        cursor += len(page.normalized_text)
        ranges.append((page, start, cursor))
    return "".join(text_parts), tuple(ranges)


def _spans_for_window(
    *,
    window_start: int,
    window_end: int,
    page_ranges: Sequence[tuple[DocumentPage, int, int]],
) -> tuple[SourceSpan, ...]:
    spans = []
    for page, page_start, page_end in page_ranges:
        overlap_start = max(window_start, page_start)
        overlap_end = min(window_end, page_end)
        if overlap_start >= overlap_end:
            continue
        spans.append(
            SourceSpan(
                file_id=page.file_id,
                pdf_page_index=page.pdf_page_index,
                char_start_in_normalized_page=overlap_start - page_start,
                char_end_in_normalized_page=overlap_end - page_start,
                page_text_hash=page.page_text_hash,
            )
        )
    return tuple(spans)


def _chunk_id(
    *,
    file_hash: str,
    extractor_fingerprint: str,
    chunker_fingerprint: str,
    source_spans: Sequence[SourceSpan],
    text_hash: str,
) -> str:
    payload = {
        "chunk_schema_version": CHUNK_SCHEMA_VERSION,
        "file_hash": file_hash,
        "extractor_fingerprint": extractor_fingerprint,
        "chunker_fingerprint": chunker_fingerprint,
        "source_spans": [span.to_dict() for span in source_spans],
        "text_hash": text_hash,
    }
    return f"chunk-{_fingerprint(payload)}"


def adapt_legacy_c0(
    document: CanonicalDocument,
    *,
    is_main: bool,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_step: int = DEFAULT_CHUNK_STEP,
    min_chunk_len: int = DEFAULT_MIN_CHUNK_LEN,
) -> tuple[ChunkRecord, ...]:
    """Reproduce legacy C0 windows and attach canonical page provenance."""
    _validate_chunk_config(chunk_size, chunk_step, min_chunk_len)
    if not isinstance(is_main, bool):
        raise TypeError("is_main must be a bool")
    chunker_fingerprint = legacy_c0_chunker_fingerprint(
        chunk_size=chunk_size,
        chunk_step=chunk_step,
        min_chunk_len=min_chunk_len,
    )
    joined_text, page_ranges = _joined_page_ranges(document.pages)
    truncated_text = truncate_final_references(joined_text)

    drafts = []
    for offset in range(0, len(truncated_text), chunk_step):
        text = truncated_text[offset : offset + chunk_size]
        if len(text) <= min_chunk_len:
            continue
        spans = _spans_for_window(
            window_start=offset,
            window_end=offset + len(text),
            page_ranges=page_ranges,
        )
        if not spans:
            raise CanonicalIRError(
                "legacy C0 produced an indexable chunk without page text"
            )
        text_hash = hash_text(text)
        drafts.append(
            ChunkRecord(
                schema_version=CHUNK_SCHEMA_VERSION,
                chunk_id=_chunk_id(
                    file_hash=document.file_hash,
                    extractor_fingerprint=document.extractor_fingerprint,
                    chunker_fingerprint=chunker_fingerprint,
                    source_spans=spans,
                    text_hash=text_hash,
                ),
                paper_id=document.paper_id,
                file_id=document.file_id,
                file_hash=document.file_hash,
                is_main=is_main,
                is_si=not is_main,
                start_page=spans[0].pdf_page_index,
                end_page=spans[-1].pdf_page_index,
                section_path=(),
                source_spans=spans,
                text=text,
                text_hash=text_hash,
                extractor_fingerprint=document.extractor_fingerprint,
                chunker_fingerprint=chunker_fingerprint,
                previous_chunk_id=None,
                next_chunk_id=None,
                extraction_warnings=tuple(
                    dict.fromkeys(
                        (
                            *document.extraction_warnings,
                            "section-path-unavailable",
                            *(
                                warning
                                for span in spans
                                for warning in document.pages[
                                    span.pdf_page_index
                                ].extraction_warnings
                            ),
                        )
                    )
                ),
            )
        )

    return tuple(
        replace(
            draft,
            previous_chunk_id=(
                drafts[index - 1].chunk_id if index > 0 else None
            ),
            next_chunk_id=(
                drafts[index + 1].chunk_id
                if index + 1 < len(drafts)
                else None
            ),
        )
        for index, draft in enumerate(drafts)
    )


def _merge_ranges(
    ranges: Iterable[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    ordered = sorted(ranges)
    if not ordered:
        return ()
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        previous_start, previous_end = merged[-1]
        if start <= previous_end:
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return tuple(merged)


def map_evidence_span_to_chunks(
    chunks: Sequence[ChunkRecord],
    evidence_span: SourceSpan,
    *,
    require_full_coverage: bool = True,
) -> tuple[str, ...]:
    """Map one stable page locator to overlapping candidate chunk IDs.

    Mapping is based only on file identity, physical page, canonical page hash,
    and character overlap. Chunk boundaries never alter the gold locator.
    """
    relevant_page_hashes = {
        span.page_text_hash
        for chunk in chunks
        for span in chunk.source_spans
        if span.file_id == evidence_span.file_id
        and span.pdf_page_index == evidence_span.pdf_page_index
    }
    if (
        relevant_page_hashes
        and evidence_span.page_text_hash not in relevant_page_hashes
    ):
        raise ProvenanceMismatchError(
            "evidence canonical page hash does not match candidate chunks"
        )

    matching_ids = []
    covered_ranges = []
    for chunk in chunks:
        chunk_matched = False
        for span in chunk.source_spans:
            if (
                span.file_id != evidence_span.file_id
                or span.pdf_page_index != evidence_span.pdf_page_index
                or span.page_text_hash != evidence_span.page_text_hash
            ):
                continue
            overlap_start = max(
                span.char_start_in_normalized_page,
                evidence_span.char_start_in_normalized_page,
            )
            overlap_end = min(
                span.char_end_in_normalized_page,
                evidence_span.char_end_in_normalized_page,
            )
            if overlap_start < overlap_end:
                covered_ranges.append((overlap_start, overlap_end))
                chunk_matched = True
        if chunk_matched:
            matching_ids.append(chunk.chunk_id)

    merged = _merge_ranges(covered_ranges)
    fully_covered = (
        len(merged) == 1
        and merged[0][0] <= evidence_span.char_start_in_normalized_page
        and merged[0][1] >= evidence_span.char_end_in_normalized_page
    )
    if require_full_coverage and not fully_covered:
        raise EvidenceMappingError(
            "evidence span is not fully covered by candidate chunks"
        )
    return tuple(matching_ids)
