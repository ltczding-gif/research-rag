"""Deterministic ResearchQA PDF and note chunking primitives.

The module is deliberately offline and side-effect free.  PDF chunks retain
half-open character spans into the canonical physical-page IR; note chunks
retain their own source spans plus every recognized native source citation.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, replace
from typing import Mapping, Sequence

from service.pdf_baseline import truncate_final_references
from service.pdf_ir import CanonicalDocument, SourceSpan, hash_text


PDF_CHUNKER_IDS = (
    "pdf-fixed-400",
    "pdf-fixed-800",
    "pdf-fixed-1200",
    "pdf-page-aware",
    "pdf-section-aware",
    "pdf-structure-aware",
    "pdf-parent-child",
)
NOTE_CHUNKER_IDS = (
    "note-whole",
    "note-section",
    "note-claim-evidence",
    "note-reviewer-concern",
)

_FIXED_CONFIGS = {
    "pdf-fixed-400": (400, 320, 80),
    "pdf-fixed-800": (800, 700, 100),
    "pdf-fixed-1200": (1200, 1000, 120),
}
_CHUNKING_REVISION = "researchqa-chunking-v2"
_HEADING_RE = re.compile(r"(?m)^(?P<line>[^\n]+)$")
_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_NUMBERED_HEADING_RE = re.compile(
    r"^(?P<number>\d+(?:\.\d+){0,4})[.)]?\s+(?P<title>\S.+?)\s*$"
)
_KNOWN_HEADING_RE = re.compile(
    r"^(abstract|introduction|background|methods?|materials(?: and methods)?|"
    r"results?|discussion|conclusions?|references|appendix|supplementary "
    r"(?:information|materials?))$",
    re.IGNORECASE,
)
_STRUCTURE_MARKER_RE = re.compile(
    r"^\s*(?:fig(?:ure)?\.?\s*\w+|table\s*\w+|eq(?:uation)?\.?\s*\w+)\b",
    re.IGNORECASE,
)
_NOTE_HEADING_RE = re.compile(r"(?m)^#{2,3}\s+.+$")
_CLAIM_HEADING_RE = re.compile(
    r"(?mi)^#{2,6}\s*(?:claim\s*)?(?P<id>C\d+)\s*[：:].*$"
)
_ANY_HEADING_RE = re.compile(r"(?m)^#{2,6}\s+.+$")
_EVIDENCE_ROW_RE = re.compile(
    r"(?mi)^\|\s*(?P<id>E\d+)\s*\|[^\n]*$"
)
_VERDICT_ROW_RE = re.compile(
    r"(?mi)^\|\s*(?P<claim>C\d+)"
    r"(?:\s*[：:][^|\n]*)?\s*\|(?P<body>[^\n]*)$"
)
_BRACKET_CITATION_RE = re.compile(r"\[(?P<body>[^\[\]\n]+)\]")
_SOURCE_LABEL_RE = re.compile(r"^(Main|SI(?:-\d+)?)\s+(.+)$", re.IGNORECASE)


def _stable_hash(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _ordered_unique(values: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


@dataclass(frozen=True)
class ResearchQAChunk:
    """One PDF chunk with canonical provenance and hierarchy metadata."""

    chunk_id: str
    config_id: str
    paper_id: str
    file_id: str
    file_hash: str
    is_main: bool
    is_si: bool
    role: str
    text: str
    text_hash: str
    source_spans: tuple[SourceSpan, ...]
    section_path: tuple[str, ...]
    previous_chunk_id: str | None
    next_chunk_id: str | None
    parent_chunk_id: str | None
    extractor_fingerprint: str
    chunker_fingerprint: str
    extraction_warnings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.config_id not in PDF_CHUNKER_IDS:
            raise ValueError(f"unknown PDF chunker: {self.config_id}")
        if self.role not in {"chunk", "child", "parent"}:
            raise ValueError(f"invalid chunk role: {self.role}")
        if self.is_main == self.is_si:
            raise ValueError("exactly one of is_main and is_si must be true")
        if not self.source_spans:
            raise ValueError("PDF chunks require at least one source span")
        if hash_text(self.text) != self.text_hash:
            raise ValueError("text_hash does not match chunk text")

    @property
    def start_page(self) -> int:
        return self.source_spans[0].pdf_page_index

    @property
    def end_page(self) -> int:
        return self.source_spans[-1].pdf_page_index

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["source_spans"] = [span.to_dict() for span in self.source_spans]
        value["section_path"] = list(self.section_path)
        value["extraction_warnings"] = list(self.extraction_warnings)
        value["start_page"] = self.start_page
        value["end_page"] = self.end_page
        return value


@dataclass(frozen=True)
class ChunkingResult:
    """Fail-closed result for one PDF chunker invocation."""

    config_id: str
    status: str
    chunks: tuple[ResearchQAChunk, ...]
    parents: tuple[ResearchQAChunk, ...] = ()
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed"}:
            raise ValueError(f"invalid chunking status: {self.status}")
        if self.status == "failed" and not self.failure_reason:
            raise ValueError("failed chunking results require failure_reason")
        if self.status == "completed" and self.failure_reason is not None:
            raise ValueError("completed chunking results cannot have failure_reason")


@dataclass(frozen=True)
class NoteSourceSpan:
    """Half-open character range in the frozen Markdown note."""

    char_start: int
    char_end: int

    def __post_init__(self) -> None:
        if self.char_start < 0 or self.char_end <= self.char_start:
            raise ValueError("note source span must be non-empty and non-negative")


@dataclass(frozen=True)
class SourceCitation:
    """One parsed native source coordinate found in a note."""

    raw: str
    source_label: str
    file_id: str | None
    coordinate_type: str
    locator: str
    page_start: int | None
    page_end: int | None
    note_span: NoteSourceSpan
    is_benchmark_pdf: bool


@dataclass(frozen=True)
class NoteChunk:
    """One deterministic note chunk with citations back to native sources."""

    chunk_id: str
    config_id: str
    paper_id: str
    note_sha256: str
    text: str
    text_hash: str
    note_spans: tuple[NoteSourceSpan, ...]
    citations: tuple[SourceCitation, ...]
    previous_chunk_id: str | None
    next_chunk_id: str | None
    claim_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    concern_id: str | None = None

    def __post_init__(self) -> None:
        if self.config_id not in NOTE_CHUNKER_IDS:
            raise ValueError(f"unknown note chunker: {self.config_id}")
        if not self.note_spans:
            raise ValueError("note chunks require at least one note source span")
        if hash_text(self.text) != self.text_hash:
            raise ValueError("text_hash does not match note chunk text")


@dataclass(frozen=True)
class NoteChunkingResult:
    """Fail-closed result for one Markdown note chunker invocation."""

    config_id: str
    status: str
    chunks: tuple[NoteChunk, ...]
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {"completed", "failed"}:
            raise ValueError(f"invalid chunking status: {self.status}")
        if self.status == "failed" and not self.failure_reason:
            raise ValueError("failed chunking results require failure_reason")


def _joined_pages(
    document: CanonicalDocument,
) -> tuple[str, tuple[tuple[object, int, int], ...]]:
    parts: list[str] = []
    ranges: list[tuple[object, int, int]] = []
    cursor = 0
    for index, page in enumerate(document.pages):
        if index:
            parts.append("\n")
            cursor += 1
        start = cursor
        parts.append(page.normalized_text)
        cursor += len(page.normalized_text)
        ranges.append((page, start, cursor))
    return "".join(parts), tuple(ranges)


def _spans_for_range(
    start: int,
    end: int,
    page_ranges: Sequence[tuple[object, int, int]],
) -> tuple[SourceSpan, ...]:
    spans = []
    for raw_page, page_start, page_end in page_ranges:
        overlap_start = max(start, page_start)
        overlap_end = min(end, page_end)
        if overlap_start >= overlap_end:
            continue
        page = raw_page
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


def _chunker_fingerprint(config_id: str) -> str:
    if config_id in _FIXED_CONFIGS:
        size, step, minimum = _FIXED_CONFIGS[config_id]
        rule: object = {
            "kind": "fixed-character",
            "size": size,
            "step": step,
            "strict_minimum": minimum,
            "reference_policy": "truncate-final-references",
        }
    elif config_id in {"pdf-page-aware", "pdf-section-aware"}:
        rule = {
            "kind": config_id.removeprefix("pdf-"),
            "target": 800,
            "hard_max": 1200,
            "paragraph_revision": 1,
        }
    elif config_id == "pdf-structure-aware":
        rule = {
            "kind": "structure-aware",
            "target": 800,
            "hard_max_non_atomic": 1200,
            "atomic": ("figure-caption", "table", "equation", "adjacent-explanation"),
            "detector_revision": 1,
        }
    elif config_id == "pdf-parent-child":
        rule = {
            "kind": "parent-child",
            "child_size": 400,
            "child_step": 320,
            "child_strict_minimum": 80,
            "parent_target": 1200,
            "parent_hard_max": 1600,
        }
    else:
        raise ValueError(f"unknown PDF chunker: {config_id}")
    return _stable_hash(
        {
            "revision": _CHUNKING_REVISION,
            "config_id": config_id,
            "rule": rule,
        }
    )


def _make_pdf_chunk(
    *,
    document: CanonicalDocument,
    is_main: bool,
    config_id: str,
    role: str,
    text: str,
    start: int,
    end: int,
    page_ranges: Sequence[tuple[object, int, int]],
    section_path: Sequence[str] = (),
    parent_chunk_id: str | None = None,
    extra_warnings: Sequence[str] = (),
) -> ResearchQAChunk:
    spans = _spans_for_range(start, end, page_ranges)
    if not spans:
        raise ValueError("indexable PDF chunk has no canonical source span")
    text_hash = hash_text(text)
    fingerprint = _chunker_fingerprint(config_id)
    chunk_id = "chunk-" + _stable_hash(
        {
            "revision": _CHUNKING_REVISION,
            "config_id": config_id,
            "role": role,
            "file_hash": document.file_hash,
            "extractor_fingerprint": document.extractor_fingerprint,
            "chunker_fingerprint": fingerprint,
            "source_spans": [span.to_dict() for span in spans],
            "text_hash": text_hash,
        }
    )
    page_warnings = [
        warning
        for span in spans
        for warning in document.pages[span.pdf_page_index].extraction_warnings
    ]
    return ResearchQAChunk(
        chunk_id=chunk_id,
        config_id=config_id,
        paper_id=document.paper_id,
        file_id=document.file_id,
        file_hash=document.file_hash,
        is_main=is_main,
        is_si=not is_main,
        role=role,
        text=text,
        text_hash=text_hash,
        source_spans=spans,
        section_path=tuple(section_path),
        previous_chunk_id=None,
        next_chunk_id=None,
        parent_chunk_id=parent_chunk_id,
        extractor_fingerprint=document.extractor_fingerprint,
        chunker_fingerprint=fingerprint,
        extraction_warnings=_ordered_unique(
            (
                *document.extraction_warnings,
                *page_warnings,
                *extra_warnings,
            )
        ),
    )


def _link_pdf_chunks(
    chunks: Sequence[ResearchQAChunk],
) -> tuple[ResearchQAChunk, ...]:
    return tuple(
        replace(
            chunk,
            previous_chunk_id=(
                chunks[index - 1].chunk_id if index else None
            ),
            next_chunk_id=(
                chunks[index + 1].chunk_id
                if index + 1 < len(chunks)
                else None
            ),
        )
        for index, chunk in enumerate(chunks)
    )


def _trim_range(text: str, start: int, end: int) -> tuple[int, int] | None:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return (start, end) if start < end else None


def _paragraph_ranges(text: str, start: int, end: int) -> tuple[tuple[int, int], ...]:
    ranges = []
    cursor = start
    for match in re.finditer(r"\n[ \t]*\n+", text[start:end]):
        split_end = start + match.start()
        trimmed = _trim_range(text, cursor, split_end)
        if trimmed is not None:
            ranges.append(trimmed)
        cursor = start + match.end()
    trimmed = _trim_range(text, cursor, end)
    if trimmed is not None:
        ranges.append(trimmed)
    return tuple(ranges)


def _indexable_ranges(
    text: str,
    ranges: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    """Drop punctuation-only extraction fragments before indexing."""

    return tuple(
        (start, end)
        for start, end in ranges
        if any(character.isalnum() for character in text[start:end])
    )


def _split_hard(
    ranges: Sequence[tuple[int, int]],
    *,
    hard_max: int,
) -> tuple[tuple[int, int], ...]:
    split = []
    for start, end in ranges:
        cursor = start
        while end - cursor > hard_max:
            split.append((cursor, cursor + hard_max))
            cursor += hard_max
        if cursor < end:
            split.append((cursor, end))
    return tuple(split)


def _aggregate_ranges(
    ranges: Sequence[tuple[int, int]],
    *,
    target: int,
    hard_max: int,
) -> tuple[tuple[int, int], ...]:
    units = _split_hard(ranges, hard_max=hard_max)
    if not units:
        return ()
    output = []
    current_start, current_end = units[0]
    for start, end in units[1:]:
        proposed_length = end - current_start
        if current_end - current_start >= target or proposed_length > hard_max:
            output.append((current_start, current_end))
            current_start, current_end = start, end
        else:
            current_end = end
    output.append((current_start, current_end))
    return tuple(output)


def _aggregate_presplit_ranges(
    ranges: Sequence[tuple[int, int]],
    *,
    target: int,
    hard_max: int,
) -> tuple[tuple[int, int], ...]:
    """Aggregate pre-split units without breaking oversized atomic units."""
    if not ranges:
        return ()
    output = []
    current_start, current_end = ranges[0]
    for start, end in ranges[1:]:
        proposed_length = end - current_start
        if (
            current_end - current_start >= target
            or proposed_length > hard_max
            or current_end - current_start > hard_max
        ):
            output.append((current_start, current_end))
            current_start, current_end = start, end
        else:
            current_end = end
    output.append((current_start, current_end))
    return tuple(output)


def _heading_records(text: str) -> tuple[tuple[int, int, str], ...]:
    records = []
    for match in _HEADING_RE.finditer(text):
        raw = match.group("line")
        line = raw.strip()
        markdown = _MARKDOWN_HEADING_RE.match(line)
        numbered = _NUMBERED_HEADING_RE.match(line)
        level = 0
        title = ""
        if markdown:
            level = len(markdown.group(1))
            title = markdown.group(2).strip()
        elif numbered and len(line) <= 140:
            level = numbered.group("number").count(".") + 1
            title = numbered.group("title").strip()
        elif _KNOWN_HEADING_RE.match(line):
            level = 1
            title = line
        elif (
            2 <= len(line) <= 100
            and any(character.isalpha() for character in line)
            and line.upper() == line
        ):
            level = 1
            title = line
        if level:
            records.append((match.start(), level, title))
    return tuple(records)


def _section_ranges(
    text: str,
) -> tuple[tuple[int, int, tuple[str, ...]], ...]:
    headings = _heading_records(text)
    if not headings:
        return ()
    sections = []
    stack: list[str] = []
    if headings[0][0] > 0 and text[: headings[0][0]].strip():
        sections.append((0, headings[0][0], ("Preamble",)))
    for index, (start, level, title) in enumerate(headings):
        while len(stack) >= level:
            stack.pop()
        stack.append(title)
        end = headings[index + 1][0] if index + 1 < len(headings) else len(text)
        sections.append((start, end, tuple(stack)))
    return tuple(sections)


def _fixed_chunks(
    document: CanonicalDocument,
    *,
    is_main: bool,
    config_id: str,
) -> ChunkingResult:
    joined, page_ranges = _joined_pages(document)
    joined = truncate_final_references(joined)
    size, step, minimum = _FIXED_CONFIGS[config_id]
    chunks = []
    for start in range(0, len(joined), step):
        text = joined[start : start + size]
        if len(text) <= minimum:
            continue
        chunks.append(
            _make_pdf_chunk(
                document=document,
                is_main=is_main,
                config_id=config_id,
                role="chunk",
                text=text,
                start=start,
                end=start + len(text),
                page_ranges=page_ranges,
                extra_warnings=("section-path-unavailable",),
            )
        )
    if not chunks:
        return ChunkingResult(
            config_id=config_id,
            status="failed",
            chunks=(),
            failure_reason="no-indexable-text",
        )
    return ChunkingResult(
        config_id=config_id,
        status="completed",
        chunks=_link_pdf_chunks(chunks),
    )


def _aware_chunks(
    document: CanonicalDocument,
    *,
    is_main: bool,
    config_id: str,
) -> ChunkingResult:
    joined, page_ranges = _joined_pages(document)
    if config_id == "pdf-page-aware":
        sections = tuple(
            (start, end, ())
            for _page, start, end in page_ranges
            if joined[start:end].strip()
        )
    else:
        sections = _section_ranges(joined)
        if not sections:
            return ChunkingResult(
                config_id=config_id,
                status="failed",
                chunks=(),
                failure_reason="section-detection-failed",
            )
    chunks = []
    for start, end, section_path in sections:
        paragraphs = _indexable_ranges(
            joined,
            _paragraph_ranges(joined, start, end),
        )
        for chunk_start, chunk_end in _aggregate_ranges(
            paragraphs,
            target=800,
            hard_max=1200,
        ):
            if not any(
                character.isalnum()
                for character in joined[chunk_start:chunk_end]
            ):
                continue
            chunks.append(
                _make_pdf_chunk(
                    document=document,
                    is_main=is_main,
                    config_id=config_id,
                    role="chunk",
                    text=joined[chunk_start:chunk_end],
                    start=chunk_start,
                    end=chunk_end,
                    page_ranges=page_ranges,
                    section_path=section_path,
                    extra_warnings=(
                        ("section-path-unavailable",)
                        if not section_path
                        else ()
                    ),
                )
            )
    if not chunks:
        return ChunkingResult(
            config_id=config_id,
            status="failed",
            chunks=(),
            failure_reason="no-indexable-text",
        )
    return ChunkingResult(
        config_id=config_id,
        status="completed",
        chunks=_link_pdf_chunks(chunks),
    )


def _is_structure_marker(block: str) -> bool:
    stripped = block.strip()
    if _STRUCTURE_MARKER_RE.match(stripped):
        return True
    if "\t" in stripped or stripped.count("|") >= 2:
        return True
    if (
        len(stripped) <= 240
        and "=" in stripped
        and re.search(r"[+\-*/^∑∫≈≤≥]", stripped)
    ):
        return True
    return False


def _structure_chunks(
    document: CanonicalDocument,
    *,
    is_main: bool,
) -> ChunkingResult:
    config_id = "pdf-structure-aware"
    joined, page_ranges = _joined_pages(document)
    sections = _section_ranges(joined) or ((0, len(joined), ()),)
    chunks = []
    detected = False
    for section_start, section_end, section_path in sections:
        paragraphs = list(
            _indexable_ranges(
                joined,
                _paragraph_ranges(joined, section_start, section_end),
            )
        )
        marker_indexes = [
            index
            for index, (start, end) in enumerate(paragraphs)
            if _is_structure_marker(joined[start:end])
        ]
        if marker_indexes:
            detected = True
        atomic_groups = []
        claimed: set[int] = set()
        for marker_index in marker_indexes:
            indexes = range(
                max(0, marker_index - 1),
                min(len(paragraphs), marker_index + 2),
            )
            new_indexes = [index for index in indexes if index not in claimed]
            if not new_indexes:
                continue
            claimed.update(new_indexes)
            atomic_groups.append(
                (
                    paragraphs[min(new_indexes)][0],
                    paragraphs[max(new_indexes)][1],
                )
            )
        regular_units = [
            paragraph
            for index, paragraph in enumerate(paragraphs)
            if index not in claimed
        ]
        units = list(_split_hard(regular_units, hard_max=1200))
        units.extend(atomic_groups)
        units.sort()
        for chunk_start, chunk_end in _aggregate_presplit_ranges(
            units,
            target=800,
            hard_max=1200,
        ):
            if not any(
                character.isalnum()
                for character in joined[chunk_start:chunk_end]
            ):
                continue
            chunks.append(
                _make_pdf_chunk(
                    document=document,
                    is_main=is_main,
                    config_id=config_id,
                    role="chunk",
                    text=joined[chunk_start:chunk_end],
                    start=chunk_start,
                    end=chunk_end,
                    page_ranges=page_ranges,
                    section_path=section_path,
                    extra_warnings=(
                        ("section-path-unavailable",)
                        if not section_path
                        else ()
                    ),
                )
            )
    if not detected:
        return ChunkingResult(
            config_id=config_id,
            status="failed",
            chunks=(),
            failure_reason="structure-detection-failed",
        )
    return ChunkingResult(
        config_id=config_id,
        status="completed",
        chunks=_link_pdf_chunks(chunks),
    )


def _bounded_parent_ranges(
    paragraphs: Sequence[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    """Choose paragraph-preferred parent boundaries in the 800-1600 band."""
    if not paragraphs:
        return ()
    document_start = paragraphs[0][0]
    document_end = paragraphs[-1][1]
    cursor = document_start
    output = []
    paragraph_starts = tuple(start for start, _end in paragraphs[1:])
    while document_end - cursor > 1600:
        lower = cursor + 800
        upper = min(cursor + 1600, document_end - 800)
        target = min(cursor + 1200, upper)
        candidates = [
            boundary
            for boundary in paragraph_starts
            if lower <= boundary <= upper
        ]
        boundary = (
            min(candidates, key=lambda value: (abs(value - target), value))
            if candidates
            else target
        )
        output.append((cursor, boundary))
        cursor = boundary
    output.append((cursor, document_end))
    return tuple(output)


def _parent_child_chunks(
    document: CanonicalDocument,
    *,
    is_main: bool,
) -> ChunkingResult:
    config_id = "pdf-parent-child"
    joined, page_ranges = _joined_pages(document)
    paragraphs = _paragraph_ranges(joined, 0, len(joined))
    parent_ranges = _bounded_parent_ranges(paragraphs)
    parents = [
        _make_pdf_chunk(
            document=document,
            is_main=is_main,
            config_id=config_id,
            role="parent",
            text=joined[start:end],
            start=start,
            end=end,
            page_ranges=page_ranges,
        )
        for start, end in parent_ranges
    ]
    children = []
    for parent, (parent_start, parent_end) in zip(parents, parent_ranges):
        for child_start in range(parent_start, parent_end, 320):
            child_end = min(child_start + 400, parent_end)
            if child_end - child_start <= 80:
                continue
            children.append(
                _make_pdf_chunk(
                    document=document,
                    is_main=is_main,
                    config_id=config_id,
                    role="child",
                    text=joined[child_start:child_end],
                    start=child_start,
                    end=child_end,
                    page_ranges=page_ranges,
                    parent_chunk_id=parent.chunk_id,
                )
            )
    if not children:
        return ChunkingResult(
            config_id=config_id,
            status="failed",
            chunks=(),
            parents=(),
            failure_reason="no-indexable-text",
        )
    return ChunkingResult(
        config_id=config_id,
        status="completed",
        chunks=_link_pdf_chunks(children),
        parents=_link_pdf_chunks(parents),
    )


def chunk_pdf(
    document: CanonicalDocument,
    config_id: str,
    *,
    is_main: bool,
) -> ChunkingResult:
    """Run one of the seven approved deterministic PDF chunkers."""
    if config_id not in PDF_CHUNKER_IDS:
        raise ValueError(f"unknown PDF chunker: {config_id}")
    if not isinstance(is_main, bool):
        raise TypeError("is_main must be a bool")
    if config_id in _FIXED_CONFIGS:
        return _fixed_chunks(document, is_main=is_main, config_id=config_id)
    if config_id in {"pdf-page-aware", "pdf-section-aware"}:
        return _aware_chunks(document, is_main=is_main, config_id=config_id)
    if config_id == "pdf-structure-aware":
        return _structure_chunks(document, is_main=is_main)
    return _parent_child_chunks(document, is_main=is_main)


def _source_file_id(
    label: str,
    source_file_ids: Mapping[str, str] | None,
) -> str | None:
    if source_file_ids is None:
        return "Main" if label.casefold() == "main" else label.upper()
    folded = label.casefold()
    for source_label, file_id in source_file_ids.items():
        if source_label.casefold() == folded:
            return file_id
    return None


def parse_source_citations(
    note_text: str,
    *,
    source_file_ids: Mapping[str, str] | None = None,
) -> tuple[SourceCitation, ...]:
    """Parse approved PDF and native SI coordinates from Markdown citations."""
    citations = []
    for bracket in _BRACKET_CITATION_RE.finditer(note_text):
        for raw_segment in bracket.group("body").split(";"):
            segment = raw_segment.strip()
            source_match = _SOURCE_LABEL_RE.match(segment)
            if source_match is None:
                continue
            label = source_match.group(1)
            locator = source_match.group(2).strip()
            file_id = _source_file_id(label, source_file_ids)
            page_match = re.search(
                r"\bpp?\.\s*(?P<pages>\d+(?:\s*-\s*\d+)?"
                r"(?:\s*,\s*\d+(?:\s*-\s*\d+)?)*)",
                locator,
                re.IGNORECASE,
            )
            if page_match:
                for page_token in page_match.group("pages").split(","):
                    values = [
                        int(value)
                        for value in re.findall(r"\d+", page_token)
                    ]
                    page_start = values[0]
                    page_end = values[-1]
                    citations.append(
                        SourceCitation(
                            raw=segment,
                            source_label=label,
                            file_id=file_id,
                            coordinate_type="pdf_page",
                            locator=page_token.strip(),
                            page_start=page_start,
                            page_end=page_end,
                            note_span=NoteSourceSpan(
                                bracket.start(),
                                bracket.end(),
                            ),
                            is_benchmark_pdf=label.casefold() == "main",
                        )
                    )
                continue
            coordinate_type = None
            for pattern, candidate_type in (
                (r"^para\.\d+", "docx_paragraph"),
                (r"^table\.\d+\s+rows\.", "docx_table"),
                (r'^sheet\."[^"]+"\s+cells\.', "xlsx_cells"),
                (r"^rows\.\d+(?:-\d+)?\s+cols\.", "csv_rows_columns"),
            ):
                if re.search(pattern, locator, re.IGNORECASE):
                    coordinate_type = candidate_type
                    break
            if coordinate_type:
                citations.append(
                    SourceCitation(
                        raw=segment,
                        source_label=label,
                        file_id=file_id,
                        coordinate_type=coordinate_type,
                        locator=locator,
                        page_start=None,
                        page_end=None,
                        note_span=NoteSourceSpan(bracket.start(), bracket.end()),
                        is_benchmark_pdf=False,
                    )
                )
    return tuple(citations)


def _note_sha256(note_text: str) -> str:
    return hashlib.sha256(note_text.encode("utf-8")).hexdigest()


def _make_note_chunk(
    *,
    note_text: str,
    paper_id: str,
    config_id: str,
    ranges: Sequence[tuple[int, int]],
    all_citations: Sequence[SourceCitation],
    claim_ids: Sequence[str] = (),
    evidence_ids: Sequence[str] = (),
    concern_id: str | None = None,
) -> NoteChunk:
    ordered_ranges = tuple(sorted(dict.fromkeys(ranges)))
    note_spans = tuple(NoteSourceSpan(start, end) for start, end in ordered_ranges)
    text = "\n".join(note_text[start:end] for start, end in ordered_ranges)
    text_hash = hash_text(text)
    note_hash = _note_sha256(note_text)
    citations = tuple(
        citation
        for citation in all_citations
        if any(
            span.char_start <= citation.note_span.char_start
            and citation.note_span.char_end <= span.char_end
            for span in note_spans
        )
    )
    normalized_claim_ids = tuple(sorted(set(claim_ids)))
    normalized_evidence_ids = tuple(sorted(set(evidence_ids)))
    chunk_id = "note-chunk-" + _stable_hash(
        {
            "revision": _CHUNKING_REVISION,
            "config_id": config_id,
            "paper_id": paper_id,
            "note_sha256": note_hash,
            "note_spans": [asdict(span) for span in note_spans],
            "text_hash": text_hash,
            "claim_ids": normalized_claim_ids,
            "evidence_ids": normalized_evidence_ids,
            "concern_id": concern_id,
        }
    )
    return NoteChunk(
        chunk_id=chunk_id,
        config_id=config_id,
        paper_id=paper_id,
        note_sha256=note_hash,
        text=text,
        text_hash=text_hash,
        note_spans=note_spans,
        citations=citations,
        previous_chunk_id=None,
        next_chunk_id=None,
        claim_ids=normalized_claim_ids,
        evidence_ids=normalized_evidence_ids,
        concern_id=concern_id,
    )


def _link_note_chunks(chunks: Sequence[NoteChunk]) -> tuple[NoteChunk, ...]:
    return tuple(
        replace(
            chunk,
            previous_chunk_id=(
                chunks[index - 1].chunk_id if index else None
            ),
            next_chunk_id=(
                chunks[index + 1].chunk_id
                if index + 1 < len(chunks)
                else None
            ),
        )
        for index, chunk in enumerate(chunks)
    )


def _block_end(text: str, start: int) -> int:
    next_heading = _ANY_HEADING_RE.search(text, start + 1)
    return next_heading.start() if next_heading else len(text)


def _evidence_rows(note_text: str) -> dict[str, tuple[int, int]]:
    return {
        match.group("id").upper(): (match.start(), match.end())
        for match in _EVIDENCE_ROW_RE.finditer(note_text)
    }


def _claim_blocks(note_text: str) -> dict[str, tuple[int, int]]:
    return {
        match.group("id").upper(): (
            match.start(),
            _block_end(note_text, match.start()),
        )
        for match in _CLAIM_HEADING_RE.finditer(note_text)
    }


def _note_whole(
    note_text: str,
    *,
    paper_id: str,
    all_citations: Sequence[SourceCitation],
) -> NoteChunkingResult:
    if not note_text:
        return NoteChunkingResult(
            config_id="note-whole",
            status="failed",
            chunks=(),
            failure_reason="empty-note",
        )
    chunk = _make_note_chunk(
        note_text=note_text,
        paper_id=paper_id,
        config_id="note-whole",
        ranges=((0, len(note_text)),),
        all_citations=all_citations,
    )
    return NoteChunkingResult(
        config_id="note-whole",
        status="completed",
        chunks=(chunk,),
    )


def _note_sections(
    note_text: str,
    *,
    paper_id: str,
    all_citations: Sequence[SourceCitation],
) -> NoteChunkingResult:
    headings = tuple(_NOTE_HEADING_RE.finditer(note_text))
    if not headings:
        return NoteChunkingResult(
            config_id="note-section",
            status="failed",
            chunks=(),
            failure_reason="markdown-section-detection-failed",
        )
    starts = [0] if headings[0].start() > 0 else []
    starts.extend(match.start() for match in headings)
    chunks = [
        _make_note_chunk(
            note_text=note_text,
            paper_id=paper_id,
            config_id="note-section",
            ranges=((start, starts[index + 1] if index + 1 < len(starts) else len(note_text)),),
            all_citations=all_citations,
        )
        for index, start in enumerate(starts)
        if note_text[start : starts[index + 1] if index + 1 < len(starts) else len(note_text)]
    ]
    return NoteChunkingResult(
        config_id="note-section",
        status="completed",
        chunks=_link_note_chunks(chunks),
    )


def _note_claim_evidence(
    note_text: str,
    *,
    paper_id: str,
    all_citations: Sequence[SourceCitation],
) -> NoteChunkingResult:
    claims = _claim_blocks(note_text)
    if not claims:
        return NoteChunkingResult(
            config_id="note-claim-evidence",
            status="failed",
            chunks=(),
            failure_reason="claim-detection-failed",
        )
    evidence = _evidence_rows(note_text)
    chunks = []
    for claim_id, claim_range in claims.items():
        claim_text = note_text[claim_range[0] : claim_range[1]]
        evidence_ids = tuple(
            sorted(set(re.findall(r"\bE\d+\b", claim_text, re.IGNORECASE)))
        )
        ranges = [claim_range]
        ranges.extend(evidence[item.upper()] for item in evidence_ids if item.upper() in evidence)
        chunks.append(
            _make_note_chunk(
                note_text=note_text,
                paper_id=paper_id,
                config_id="note-claim-evidence",
                ranges=ranges,
                all_citations=all_citations,
                claim_ids=(claim_id,),
                evidence_ids=evidence_ids,
            )
        )
    return NoteChunkingResult(
        config_id="note-claim-evidence",
        status="completed",
        chunks=_link_note_chunks(chunks),
    )


def _line_ranges_matching(
    note_text: str,
    pattern: re.Pattern[str],
) -> tuple[tuple[int, int], ...]:
    return tuple(
        (match.start(), match.end())
        for match in pattern.finditer(note_text)
    )


def _note_reviewer_concerns(
    note_text: str,
    *,
    paper_id: str,
    all_citations: Sequence[SourceCitation],
) -> NoteChunkingResult:
    claims = _claim_blocks(note_text)
    evidence = _evidence_rows(note_text)
    chunks = []
    for row in _VERDICT_ROW_RE.finditer(note_text):
        body = row.group("body")
        if not re.search(r"\|\s*major\s*\|?\s*$", body, re.IGNORECASE):
            continue
        claim_id = row.group("claim").upper()
        evidence_ids = tuple(
            sorted(set(match.upper() for match in re.findall(r"\bE\d+\b", body, re.IGNORECASE)))
        )
        ranges = [(row.start(), row.end())]
        if claim_id in claims:
            ranges.append(claims[claim_id])
        ranges.extend(evidence[item] for item in evidence_ids if item in evidence)
        claim_pattern = re.compile(
            rf"(?mi)^(?!\|)[^\n]*\b{re.escape(claim_id)}\b[^\n]*$"
        )
        decisive_pattern = re.compile(
            rf"(?mi)^\s*-\s*对\s*{re.escape(claim_id)}\s*[：:][^\n]*$"
        )
        candidate_lines = _line_ranges_matching(note_text, claim_pattern)
        ranges.extend(
            item
            for item in candidate_lines
            if item[0] >= row.end()
        )
        ranges.extend(_line_ranges_matching(note_text, decisive_pattern))
        chunks.append(
            _make_note_chunk(
                note_text=note_text,
                paper_id=paper_id,
                config_id="note-reviewer-concern",
                ranges=ranges,
                all_citations=all_citations,
                claim_ids=(claim_id,),
                evidence_ids=evidence_ids,
                concern_id=f"concern-{claim_id.lower()}",
            )
        )
    return NoteChunkingResult(
        config_id="note-reviewer-concern",
        status="completed",
        chunks=_link_note_chunks(chunks),
    )


def chunk_note(
    note_text: str,
    config_id: str,
    *,
    paper_id: str,
    source_file_ids: Mapping[str, str] | None = None,
) -> NoteChunkingResult:
    """Run one of the four approved deterministic Markdown note chunkers."""
    if config_id not in NOTE_CHUNKER_IDS:
        raise ValueError(f"unknown note chunker: {config_id}")
    if not paper_id or paper_id.strip() != paper_id:
        raise ValueError("paper_id must be a non-empty trimmed string")
    citations = parse_source_citations(
        note_text,
        source_file_ids=source_file_ids,
    )
    if config_id == "note-whole":
        return _note_whole(
            note_text,
            paper_id=paper_id,
            all_citations=citations,
        )
    if config_id == "note-section":
        return _note_sections(
            note_text,
            paper_id=paper_id,
            all_citations=citations,
        )
    if config_id == "note-claim-evidence":
        return _note_claim_evidence(
            note_text,
            paper_id=paper_id,
            all_citations=citations,
        )
    return _note_reviewer_concerns(
        note_text,
        paper_id=paper_id,
        all_citations=citations,
    )


def note_chunk_pdf_backlinks(
    note_chunk: NoteChunk,
    pdf_chunks: Sequence[ResearchQAChunk],
) -> tuple[str, ...]:
    """Resolve valid Main PDF page citations to overlapping PDF chunks."""
    page_ranges = [
        (citation.page_start, citation.page_end)
        for citation in note_chunk.citations
        if citation.is_benchmark_pdf
        and citation.page_start is not None
        and citation.page_end is not None
    ]
    if not page_ranges:
        return ()
    matches = []
    for chunk in pdf_chunks:
        if not chunk.is_main:
            continue
        physical_start = chunk.start_page + 1
        physical_end = chunk.end_page + 1
        if any(
            physical_start <= cited_end and cited_start <= physical_end
            for cited_start, cited_end in page_ranges
        ):
            matches.append(chunk.chunk_id)
    return tuple(matches)


__all__ = [
    "ChunkingResult",
    "NOTE_CHUNKER_IDS",
    "NoteChunk",
    "NoteChunkingResult",
    "NoteSourceSpan",
    "PDF_CHUNKER_IDS",
    "ResearchQAChunk",
    "SourceCitation",
    "chunk_note",
    "chunk_pdf",
    "note_chunk_pdf_backlinks",
    "parse_source_citations",
]
