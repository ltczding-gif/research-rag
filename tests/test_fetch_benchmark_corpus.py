from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.scripts import fetch_corpus


def _manifest_record(artifact_path: str, digest: str) -> dict:
    file_record = {
        "file_id": "paper-1-main",
        "artifact_path": artifact_path,
        "source_url": "https://example.org/paper.pdf",
        "sha256": digest,
    }
    return {
        "paper_id": "paper-1",
        "main_pdf": file_record,
        "si": [],
    }


def test_resolve_artifact_path_rejects_escape(tmp_path: Path):
    with pytest.raises(ValueError, match="escapes corpus root"):
        fetch_corpus.resolve_artifact_path(tmp_path, "../private.pdf")


def test_verify_pdf_checks_magic_and_sha256(tmp_path: Path):
    payload = b"%PDF-1.7 benchmark fixture"
    path = tmp_path / "paper.pdf"
    path.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()

    assert fetch_corpus.verify_pdf(path, digest) == (True, digest)
    valid, detail = fetch_corpus.verify_pdf(path, "0" * 64)
    assert not valid
    assert "sha256 mismatch" in detail


def test_check_only_accepts_checksum_pinned_local_file(tmp_path: Path):
    corpus_root = tmp_path / "corpus"
    target = corpus_root / "files" / "paper.pdf"
    target.parent.mkdir(parents=True)
    payload = b"%PDF-1.7 benchmark fixture"
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    manifest = corpus_root / "manifest.jsonl"
    manifest.write_text(
        json.dumps(_manifest_record("files/paper.pdf", digest)) + "\n",
        encoding="utf-8",
    )

    status = fetch_corpus.run(
        manifest_path=manifest,
        corpus_root=corpus_root,
        paper_ids=None,
        check_only=True,
        force=False,
        timeout=1,
    )

    assert status == 0
