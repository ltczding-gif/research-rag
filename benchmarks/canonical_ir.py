"""Manifest-driven Wave 1A canonical IR build entry point.

This module reads only the public benchmark manifest and checksum-pinned PDF
paths supplied by the caller. It does not access Zotero, user notes, ChromaDB,
or production ledgers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable

from service.pdf_ir import (
    CanonicalDocument,
    ChunkRecord,
    adapt_legacy_c0,
    extract_pdf_document,
)


Extractor = Callable[..., CanonicalDocument]


@dataclass(frozen=True)
class CanonicalIRBuild:
    """Documents and C0 chunks produced from one manifest snapshot."""

    documents: tuple[CanonicalDocument, ...]
    chunks: tuple[ChunkRecord, ...]

    def summary(self) -> dict[str, int]:
        return {
            "papers": len({document.paper_id for document in self.documents}),
            "files": len(self.documents),
            "pages": sum(len(document.pages) for document in self.documents),
            "chunks": len(self.chunks),
            "main_chunks": sum(chunk.is_main for chunk in self.chunks),
            "si_chunks": sum(chunk.is_si for chunk in self.chunks),
            "warnings": sum(
                len(chunk.extraction_warnings) for chunk in self.chunks
            ),
        }


def _load_manifest(path: Path) -> tuple[dict[str, object], ...]:
    records = []
    seen_papers: set[str] = set()
    seen_files: set[str] = set()
    for line_number, raw in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(),
        1,
    ):
        if not raw.strip():
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"{path}:{line_number}: invalid JSON: {exc.msg}"
            ) from exc
        if not isinstance(record, dict):
            raise ValueError(f"{path}:{line_number}: record must be an object")
        paper_id = record.get("paper_id")
        if not isinstance(paper_id, str) or not paper_id:
            raise ValueError(f"{path}:{line_number}: missing paper_id")
        if paper_id in seen_papers:
            raise ValueError(f"{path}:{line_number}: duplicate paper_id {paper_id}")
        seen_papers.add(paper_id)

        main_pdf = record.get("main_pdf")
        si_files = record.get("si")
        if not isinstance(main_pdf, dict) or not isinstance(si_files, list):
            raise ValueError(
                f"{path}:{line_number}: main_pdf/si contract is invalid"
            )
        for file_record in (main_pdf, *si_files):
            if not isinstance(file_record, dict):
                raise ValueError(
                    f"{path}:{line_number}: PDF record must be an object"
                )
            file_id = file_record.get("file_id")
            if not isinstance(file_id, str) or not file_id:
                raise ValueError(f"{path}:{line_number}: missing file_id")
            if file_id in seen_files:
                raise ValueError(
                    f"{path}:{line_number}: duplicate file_id {file_id}"
                )
            seen_files.add(file_id)
        records.append(record)
    return tuple(records)


def _resolve_artifact(corpus_root: Path, artifact_path: object) -> Path:
    if not isinstance(artifact_path, str) or not artifact_path:
        raise ValueError("artifact_path must be a non-empty relative path")
    relative = Path(artifact_path)
    if relative.is_absolute() or PureWindowsPath(artifact_path).is_absolute():
        raise ValueError(f"unsafe artifact_path: {artifact_path}")
    root = corpus_root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"unsafe artifact_path: {artifact_path}") from exc
    return candidate


def build_manifest_c0(
    manifest_path: str | Path,
    corpus_root: str | Path,
    *,
    extractor: Extractor = extract_pdf_document,
) -> CanonicalIRBuild:
    """Build page IR and legacy C0 chunks for every main/SI manifest file."""
    manifest = Path(manifest_path)
    root = Path(corpus_root)
    documents = []
    chunks = []
    for record in _load_manifest(manifest):
        paper_id = str(record["paper_id"])
        file_specs = [(record["main_pdf"], True)]
        file_specs.extend((item, False) for item in record["si"])
        for file_record, is_main in file_specs:
            document = extractor(
                _resolve_artifact(root, file_record["artifact_path"]),
                paper_id=paper_id,
                file_id=file_record["file_id"],
                expected_file_hash=file_record["sha256"],
            )
            documents.append(document)
            chunks.extend(adapt_legacy_c0(document, is_main=is_main))
    return CanonicalIRBuild(
        documents=tuple(documents),
        chunks=tuple(chunks),
    )
