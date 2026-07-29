from __future__ import annotations

import json
from pathlib import Path

import pytest

from benchmarks import canonical_ir
from service.pdf_ir import (
    CanonicalDocument,
    DocumentPage,
    DEFAULT_EXTRACTOR_FINGERPRINT,
)


def _file(file_id: str, artifact_path: str, sha256: str) -> dict[str, str]:
    return {
        "file_id": file_id,
        "artifact_path": artifact_path,
        "sha256": sha256,
    }


def _write_manifest(path: Path, *, artifact_path: str = "files/main.pdf") -> None:
    record = {
        "paper_id": "paper-1",
        "main_pdf": _file("paper-1-main", artifact_path, "a" * 64),
        "si": [_file("paper-1-si-1", "files/si.pdf", "b" * 64)],
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")


def test_manifest_builds_main_then_si_and_forwards_hashes(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_manifest(manifest)
    calls = []

    def fake_extractor(
        path,
        *,
        paper_id,
        file_id,
        expected_file_hash,
    ):
        calls.append((path, paper_id, file_id, expected_file_hash))
        page = DocumentPage.create(
            paper_id=paper_id,
            file_id=file_id,
            pdf_page_index=0,
            text=("m" if file_id.endswith("main") else "s") * 900,
        )
        return CanonicalDocument(
            paper_id=paper_id,
            file_id=file_id,
            file_hash=expected_file_hash,
            extractor_fingerprint=DEFAULT_EXTRACTOR_FINGERPRINT,
            pages=(page,),
        )

    result = canonical_ir.build_manifest_c0(
        manifest,
        corpus,
        extractor=fake_extractor,
    )

    assert [call[2] for call in calls] == ["paper-1-main", "paper-1-si-1"]
    assert [call[3] for call in calls] == ["a" * 64, "b" * 64]
    assert calls[0][0] == (corpus / "files" / "main.pdf").resolve()
    assert result.documents[0].file_id == "paper-1-main"
    assert all(chunk.is_main for chunk in result.chunks[:2])
    assert all(chunk.is_si for chunk in result.chunks[2:])
    assert result.summary() == {
        "papers": 1,
        "files": 2,
        "pages": 2,
        "chunks": 4,
        "main_chunks": 2,
        "si_chunks": 2,
        "warnings": 4,
    }


@pytest.mark.parametrize(
    "unsafe_path",
    ["../private.pdf", "/private.pdf", r"C:\private.pdf"],
)
def test_manifest_rejects_paths_outside_corpus(tmp_path, unsafe_path):
    manifest = tmp_path / "manifest.jsonl"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write_manifest(manifest, artifact_path=unsafe_path)

    with pytest.raises(ValueError, match="unsafe artifact_path"):
        canonical_ir.build_manifest_c0(
            manifest,
            corpus,
            extractor=lambda *args, **kwargs: None,
        )


def test_manifest_rejects_duplicate_file_identity(tmp_path):
    manifest = tmp_path / "manifest.jsonl"
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    record = {
        "paper_id": "paper-1",
        "main_pdf": _file("same-file", "files/main.pdf", "a" * 64),
        "si": [_file("same-file", "files/si.pdf", "b" * 64)],
    }
    manifest.write_text(json.dumps(record) + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate file_id"):
        canonical_ir.build_manifest_c0(
            manifest,
            corpus,
            extractor=lambda *args, **kwargs: None,
        )
