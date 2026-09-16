"""Strict PDF source inventory and canonical ingest preparation."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Callable, Literal

import yaml

try:
    from .pdf_ir import (
        CanonicalDocument,
        ChunkRecord,
        SourceSpan,
        adapt_legacy_c0,
        extract_pdf_document,
        hash_file,
        hash_text,
        locate_canonical_text,
    )
except ImportError:  # Existing flat service import convention.
    from pdf_ir import (
        CanonicalDocument,
        ChunkRecord,
        SourceSpan,
        adapt_legacy_c0,
        extract_pdf_document,
        hash_file,
        hash_text,
        locate_canonical_text,
    )


SourceRole = Literal["main", "si"]
SourceStatus = Literal["success", "empty", "missing", "error"]
_PDF_PATH_KEY = re.compile(r"^pdf_(\d+)_path$")
_ZOTERO_KEY = re.compile(r"^[A-Za-z0-9]{8}$")
_STORAGE_KEY = re.compile(r"[\\/]storage[\\/]([A-Za-z0-9]{8})[\\/]")


class PdfSourceError(ValueError):
    """The declared PDF source set cannot be prepared safely."""


@dataclass(frozen=True)
class DeclaredPdfSource:
    note_path: Path
    note_sha256: str
    pdf_index: int
    source_role: SourceRole
    declared_path: Path | None
    original_path: Path | None
    declared_parent_key: str | None
    declared_attachment_key: str | None


@dataclass(frozen=True)
class AttachmentIdentity:
    parent_key: str
    attachment_key: str
    zotero_path: str
    identity_source: str


@dataclass(frozen=True)
class PdfSourceRecord:
    source_id: str | None
    content_hash: str | None
    path: str | None
    note_path: str
    note_sha256: str
    pdf_index: int
    source_role: SourceRole
    status: SourceStatus
    error_code: str | None
    declared_path: str | None
    original_path: str | None
    pdf_filename: str | None
    paper_id: str | None
    file_id: str | None
    zotero_parent_key: str | None
    zotero_attachment_key: str | None
    identity_source: str
    file_sha256: str | None
    size_bytes: int | None
    page_count: int | None
    extractor_fingerprint: str | None
    page_text_hashes: tuple[str, ...]
    chunk_count: int
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["page_text_hashes"] = list(self.page_text_hashes)
        value["warnings"] = list(self.warnings)
        return value


@dataclass(frozen=True)
class PreparedPdfBuild:
    inventory: tuple[PdfSourceRecord, ...]
    documents: tuple[CanonicalDocument, ...]
    chunks: tuple[ChunkRecord, ...]
    source_set_fingerprint: str

    @property
    def publishable(self) -> bool:
        if not self.inventory or any(
            item.status != "success" for item in self.inventory
        ):
            return False
        note_paths = {item.note_path for item in self.inventory}
        main_notes = {
            item.note_path
            for item in self.inventory
            if item.pdf_index == 0 and item.source_role == "main"
        }
        return note_paths == main_notes


def _text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _path(value: object) -> Path | None:
    normalized = _text(value)
    return Path(normalized).expanduser() if normalized else None


def _frontmatter(path: Path) -> dict[str, object] | None:
    text = path.read_text(encoding="utf-8-sig")
    match = re.match(r"^---\s*\r?\n(.*?)\r?\n---(?:\s*\r?\n|$)", text, re.DOTALL)
    if not match:
        return None
    value = yaml.safe_load(match.group(1)) or {}
    if not isinstance(value, dict):
        raise PdfSourceError(f"frontmatter must be a mapping: {path}")
    return value


def discover_declared_pdf_sources(notes_dir: str | Path) -> tuple[DeclaredPdfSource, ...]:
    root = Path(notes_dir)
    if not root.is_dir():
        raise PdfSourceError(f"notes directory is unavailable: {root}")
    declarations: list[DeclaredPdfSource] = []
    for note_path in sorted(root.glob("*.md"), key=lambda value: str(value).casefold()):
        metadata = _frontmatter(note_path)
        if metadata is None:
            continue
        indices = sorted(
            int(match.group(1))
            for key in metadata
            if (match := _PDF_PATH_KEY.fullmatch(str(key)))
        )
        note_hash = hash_file(note_path)
        for index in indices:
            declared_path = _path(metadata.get(f"pdf_{index}_path"))
            original_path = _path(metadata.get(f"pdf_{index}_source_path"))
            declarations.append(
                DeclaredPdfSource(
                    note_path=note_path.resolve(),
                    note_sha256=note_hash,
                    pdf_index=index,
                    source_role="main" if index == 0 else "si",
                    declared_path=declared_path,
                    original_path=original_path or declared_path,
                    declared_parent_key=_text(metadata.get("zotero_parent_key")),
                    declared_attachment_key=_text(
                        metadata.get(f"pdf_{index}_attachment_key")
                    ),
                )
            )
    if not declarations:
        raise PdfSourceError(f"no declared PDF sources found in: {root}")
    return tuple(declarations)


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _one_identity(
    connection: sqlite3.Connection,
    sql: str,
    parameters: tuple[str, ...],
    identity_source: str,
) -> AttachmentIdentity | None:
    rows = connection.execute(sql, parameters).fetchall()
    if len(rows) != 1:
        return None
    parent_key, attachment_key, zotero_path = rows[0]
    return AttachmentIdentity(
        parent_key=parent_key,
        attachment_key=attachment_key,
        zotero_path=zotero_path or "",
        identity_source=identity_source,
    )


def resolve_attachment_identity_exact(
    source: DeclaredPdfSource,
    *,
    zotero_db: Path | None,
    zotero_data_dir: Path | None = None,
    linked_attachment_base: Path | None = None,
) -> AttachmentIdentity | None:
    """Resolve one attachment without basename or fuzzy matching."""
    del zotero_data_dir  # Reserved for deployment provenance; no fuzzy path use.
    parent = source.declared_parent_key
    attachment = source.declared_attachment_key
    explicit_pair = bool(
        parent
        and attachment
        and _ZOTERO_KEY.fullmatch(parent)
        and _ZOTERO_KEY.fullmatch(attachment)
    )
    if zotero_db is None:
        if explicit_pair:
            return AttachmentIdentity(
                parent_key=parent,
                attachment_key=attachment,
                zotero_path=str(source.original_path or source.declared_path or ""),
                identity_source="frontmatter_exact",
            )
        return None
    try:
        with _read_only_connection(Path(zotero_db)) as connection:
            if explicit_pair:
                return _one_identity(
                    connection,
                    """
                    SELECT p.key, a.key, ia.path
                    FROM itemAttachments ia
                    JOIN items a ON a.itemID = ia.itemID
                    JOIN items p ON p.itemID = ia.parentItemID
                    WHERE a.key = ? AND p.key = ?
                    """,
                    (attachment, parent),
                    "frontmatter_exact",
                )
            if attachment or (parent and not _ZOTERO_KEY.fullmatch(parent)):
                return None

            original = source.original_path or source.declared_path
            storage_match = _STORAGE_KEY.search(str(original)) if original else None
            if storage_match:
                if parent:
                    return _one_identity(
                        connection,
                        """
                        SELECT p.key, a.key, ia.path
                        FROM itemAttachments ia
                        JOIN items a ON a.itemID = ia.itemID
                        JOIN items p ON p.itemID = ia.parentItemID
                        WHERE a.key = ? AND p.key = ?
                        """,
                        (storage_match.group(1), parent),
                        "zotero_storage_key_exact",
                    )
                return _one_identity(
                    connection,
                    """
                    SELECT p.key, a.key, ia.path
                    FROM itemAttachments ia
                    JOIN items a ON a.itemID = ia.itemID
                    JOIN items p ON p.itemID = ia.parentItemID
                    WHERE a.key = ?
                    """,
                    (storage_match.group(1),),
                    "zotero_storage_key_exact",
                )

            if original is None or linked_attachment_base is None:
                return None
            try:
                relative = original.resolve().relative_to(
                    Path(linked_attachment_base).resolve()
                )
            except (OSError, ValueError):
                return None
            zotero_path = "attachments:" + relative.as_posix()
            if parent:
                return _one_identity(
                    connection,
                    """
                    SELECT p.key, a.key, ia.path
                    FROM itemAttachments ia
                    JOIN items a ON a.itemID = ia.itemID
                    JOIN items p ON p.itemID = ia.parentItemID
                    WHERE ia.path = ? AND p.key = ?
                    """,
                    (zotero_path, parent),
                    "zotero_linked_path_exact",
                )
            return _one_identity(
                connection,
                """
                SELECT p.key, a.key, ia.path
                FROM itemAttachments ia
                JOIN items a ON a.itemID = ia.itemID
                JOIN items p ON p.itemID = ia.parentItemID
                WHERE ia.path = ?
                """,
                (zotero_path,),
                "zotero_linked_path_exact",
            )
    except sqlite3.Error:
        return None


def _record(
    source: DeclaredPdfSource,
    identity: AttachmentIdentity | None,
    *,
    status: SourceStatus,
    error_code: str | None,
    file_hash: str | None = None,
    size_bytes: int | None = None,
    document: CanonicalDocument | None = None,
    chunk_count: int = 0,
    warnings: tuple[str, ...] = (),
) -> PdfSourceRecord:
    paper_id = f"zp-{identity.parent_key.lower()}" if identity else None
    file_id = f"za-{identity.attachment_key.lower()}" if identity else None
    original = source.original_path or source.declared_path
    return PdfSourceRecord(
        source_id=file_id,
        content_hash=file_hash,
        path=str(source.declared_path) if source.declared_path else None,
        note_path=str(source.note_path),
        note_sha256=source.note_sha256,
        pdf_index=source.pdf_index,
        source_role=source.source_role,
        status=status,
        error_code=error_code,
        declared_path=str(source.declared_path) if source.declared_path else None,
        original_path=str(source.original_path) if source.original_path else None,
        pdf_filename=original.name if original else None,
        paper_id=paper_id,
        file_id=file_id,
        zotero_parent_key=identity.parent_key if identity else source.declared_parent_key,
        zotero_attachment_key=(
            identity.attachment_key if identity else source.declared_attachment_key
        ),
        identity_source=identity.identity_source if identity else "unresolved",
        file_sha256=file_hash,
        size_bytes=size_bytes,
        page_count=len(document.pages) if document else None,
        extractor_fingerprint=document.extractor_fingerprint if document else None,
        page_text_hashes=(
            tuple(page.page_text_hash for page in document.pages) if document else ()
        ),
        chunk_count=chunk_count,
        warnings=warnings,
    )


def _fingerprint(inventory: list[PdfSourceRecord]) -> str:
    semantic = sorted(
        (
            item.paper_id or "",
            item.file_id or "",
            item.pdf_index,
            item.source_role,
            item.status,
            item.file_sha256,
            item.extractor_fingerprint,
        )
        for item in inventory
    )
    payload = json.dumps(semantic, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _json_fingerprint(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _document_layout(
    document: CanonicalDocument,
) -> tuple[str, dict[int, int]]:
    parts: list[str] = []
    offsets: dict[int, int] = {}
    cursor = 0
    for index, page in enumerate(document.pages):
        if index:
            parts.append("\n")
            cursor += 1
        offsets[index] = cursor
        parts.append(page.normalized_text)
        cursor += len(page.normalized_text)
    return "".join(parts), offsets


def canonical_chunk_start(
    document: CanonicalDocument,
    chunk: ChunkRecord,
) -> int:
    """Locate and verify a chunk, including derived inter-page newlines."""
    try:
        return locate_canonical_text(
            [asdict(page) for page in document.pages], chunk.text,
            [span.to_dict() for span in chunk.source_spans],
        )
    except ValueError as exc:
        raise PdfSourceError(f"{chunk.chunk_id}: {exc}") from exc


def split_prepared_chunks_for_embedding(
    plan: PreparedPdfBuild,
    splitter: Callable[[str], list[tuple[int, int, str]]],
) -> PreparedPdfBuild:
    """Split canonical chunks into complete embedding inputs without text loss."""
    documents = {document.file_id: document for document in plan.documents}
    by_file: dict[str, list[tuple[int, ChunkRecord]]] = {}
    for chunk in plan.chunks:
        document = documents[chunk.file_id]
        absolute_start = canonical_chunk_start(document, chunk)
        by_file.setdefault(chunk.file_id, []).append((absolute_start, chunk))

    final_chunks: list[ChunkRecord] = []
    per_file_counts: dict[str, int] = {}
    for file_id in sorted(by_file):
        document = documents[file_id]
        _, page_offsets = _document_layout(document)
        drafts: list[ChunkRecord] = []
        for base_start, chunk in sorted(
            by_file[file_id], key=lambda item: (item[0], item[1].chunk_id)
        ):
            intervals = list(splitter(chunk.text))
            if not intervals:
                raise PdfSourceError(
                    f"embedding splitter returned no units: {chunk.chunk_id}"
                )
            if intervals == [(0, len(chunk.text), chunk.text)]:
                drafts.append(
                    replace(
                        chunk,
                        previous_chunk_id=None,
                        next_chunk_id=None,
                    )
                )
                continue
            cursor_in_chunk = 0
            for relative_start, relative_end, text in intervals:
                if (
                    relative_start != cursor_in_chunk
                    or relative_end <= relative_start
                    or chunk.text[relative_start:relative_end] != text
                ):
                    raise PdfSourceError(
                        f"embedding splitter did not preserve contiguous text: {chunk.chunk_id}"
                    )
                cursor_in_chunk = relative_end
                absolute_start = base_start + relative_start
                absolute_end = base_start + relative_end
                spans = []
                for page in document.pages:
                    page_start = page_offsets[page.pdf_page_index]
                    page_end = page_start + len(page.normalized_text)
                    overlap_start = max(absolute_start, page_start)
                    overlap_end = min(absolute_end, page_end)
                    if overlap_start < overlap_end:
                        spans.append(
                            SourceSpan(
                                file_id=file_id,
                                pdf_page_index=page.pdf_page_index,
                                char_start_in_normalized_page=overlap_start - page_start,
                                char_end_in_normalized_page=overlap_end - page_start,
                                page_text_hash=page.page_text_hash,
                            )
                        )
                if not spans:
                    # Only derived page separators lie outside every page.
                    # They contain no source evidence and need no vector of
                    # their own; adjacent source-bearing units retain all text.
                    if text and not text.strip("\n"):
                        continue
                    raise PdfSourceError(f"embedding unit has no canonical page text: {chunk.chunk_id}")
                chunker_fingerprint = _json_fingerprint(
                    {
                        "adapter": "complete-embedding-window-v1",
                        "base_chunker_fingerprint": chunk.chunker_fingerprint,
                        "input_policy": "contiguous-no-tail-loss",
                    }
                )
                text_hash = hash_text(text)
                chunk_id = "chunk-" + _json_fingerprint(
                    {
                        "base_chunk_id": chunk.chunk_id,
                        "chunker_fingerprint": chunker_fingerprint,
                        "relative_interval": [relative_start, relative_end],
                        "source_spans": [span.to_dict() for span in spans],
                        "text_hash": text_hash,
                    }
                )
                drafts.append(
                    replace(
                        chunk,
                        chunk_id=chunk_id,
                        start_page=spans[0].pdf_page_index,
                        end_page=spans[-1].pdf_page_index,
                        source_spans=tuple(spans),
                        text=text,
                        text_hash=text_hash,
                        chunker_fingerprint=chunker_fingerprint,
                        previous_chunk_id=None,
                        next_chunk_id=None,
                        extraction_warnings=tuple(
                            dict.fromkeys(
                                (*chunk.extraction_warnings, "embedding-window-split")
                            )
                        ),
                    )
                )
            if cursor_in_chunk != len(chunk.text):
                raise PdfSourceError(
                    f"embedding splitter dropped chunk tail: {chunk.chunk_id}"
                )
        chained = [
            replace(
                chunk,
                previous_chunk_id=drafts[index - 1].chunk_id if index else None,
                next_chunk_id=(
                    drafts[index + 1].chunk_id
                    if index + 1 < len(drafts)
                    else None
                ),
            )
            for index, chunk in enumerate(drafts)
        ]
        final_chunks.extend(chained)
        per_file_counts[file_id] = len(chained)

    inventory = tuple(
        replace(
            item,
            chunk_count=per_file_counts.get(item.file_id, item.chunk_count),
        )
        for item in plan.inventory
    )
    return replace(plan, inventory=inventory, chunks=tuple(final_chunks))


def prepare_pdf_build(
    notes_dir: str | Path,
    *,
    zotero_db: Path | None,
    zotero_data_dir: Path | None = None,
    linked_attachment_base: Path | None = None,
    chunk_size: int,
    chunk_step: int,
    min_chunk_len: int,
    extractor: Callable[..., CanonicalDocument] = extract_pdf_document,
    chunker: Callable[..., tuple[ChunkRecord, ...]] = adapt_legacy_c0,
) -> PreparedPdfBuild:
    inventory: list[PdfSourceRecord] = []
    documents: list[CanonicalDocument] = []
    chunks: list[ChunkRecord] = []
    for source in discover_declared_pdf_sources(notes_dir):
        identity = resolve_attachment_identity_exact(
            source,
            zotero_db=zotero_db,
            zotero_data_dir=zotero_data_dir,
            linked_attachment_base=linked_attachment_base,
        )
        if identity is None:
            inventory.append(
                _record(source, None, status="error", error_code="identity_unresolved")
            )
            continue
        path = source.declared_path
        if path is None or not path.is_file():
            inventory.append(
                _record(source, identity, status="missing", error_code="source_missing")
            )
            continue
        try:
            file_hash = hash_file(path)
            size_bytes = path.stat().st_size
        except OSError:
            inventory.append(
                _record(source, identity, status="error", error_code="source_hash_error")
            )
            continue

        warnings: list[str] = []
        original = source.original_path
        if original is not None and original.resolve() != path.resolve():
            if original.is_file():
                try:
                    original_hash = hash_file(original)
                except OSError:
                    inventory.append(
                        _record(
                            source,
                            identity,
                            status="error",
                            error_code="source_original_hash_error",
                            file_hash=file_hash,
                            size_bytes=size_bytes,
                        )
                    )
                    continue
                if original_hash != file_hash:
                    inventory.append(
                        _record(
                            source,
                            identity,
                            status="error",
                            error_code="source_copy_hash_mismatch",
                            file_hash=file_hash,
                            size_bytes=size_bytes,
                        )
                    )
                    continue
            else:
                warnings.append("source-original-unavailable")
        paper_id = f"zp-{identity.parent_key.lower()}"
        file_id = f"za-{identity.attachment_key.lower()}"
        try:
            document = extractor(
                path,
                paper_id=paper_id,
                file_id=file_id,
                expected_file_hash=file_hash,
            )
            source_chunks = tuple(
                chunker(
                    document,
                    is_main=source.source_role == "main",
                    chunk_size=chunk_size,
                    chunk_step=chunk_step,
                    min_chunk_len=min_chunk_len,
                )
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception:
            inventory.append(
                _record(
                    source,
                    identity,
                    status="error",
                    error_code="canonical_extraction_error",
                    file_hash=file_hash,
                    size_bytes=size_bytes,
                    warnings=tuple(warnings),
                )
            )
            continue
        if not any(page.normalized_text.strip() for page in document.pages) or not source_chunks:
            inventory.append(
                _record(
                    source,
                    identity,
                    status="empty",
                    error_code="no_indexable_text",
                    file_hash=file_hash,
                    size_bytes=size_bytes,
                    document=document,
                    warnings=tuple(warnings),
                )
            )
            continue
        inventory.append(
            _record(
                source,
                identity,
                status="success",
                error_code=None,
                file_hash=file_hash,
                size_bytes=size_bytes,
                document=document,
                chunk_count=len(source_chunks),
                warnings=tuple(warnings),
            )
        )
        documents.append(document)
        chunks.extend(source_chunks)

    inventory.sort(key=lambda item: (item.paper_id or "", item.note_path.casefold(), item.pdf_index))
    documents.sort(key=lambda item: (item.paper_id, item.file_id))
    chunks.sort(key=lambda item: (item.paper_id, item.file_id, item.start_page, item.chunk_id))
    return PreparedPdfBuild(
        inventory=tuple(inventory),
        documents=tuple(documents),
        chunks=tuple(chunks),
        source_set_fingerprint=_fingerprint(inventory),
    )
