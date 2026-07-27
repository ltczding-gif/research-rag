from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO_ROOT / "service"
sys.path.insert(0, str(SERVICE_DIR))

import pdf_baseline  # noqa: E402
import pdf_ir  # noqa: E402


class _FakePage:
    def __init__(self, text: str | None):
        self._text = text

    def extract_text(self):
        return self._text


class _FakePdf:
    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


def _document(
    *texts: str,
    paper_id: str = "paper-1",
    file_id: str = "paper-1-main",
    file_hash: str = "a" * 64,
) -> pdf_ir.CanonicalDocument:
    pages = tuple(
        pdf_ir.DocumentPage.create(
            paper_id=paper_id,
            file_id=file_id,
            pdf_page_index=index,
            text=text,
        )
        for index, text in enumerate(texts)
    )
    return pdf_ir.CanonicalDocument(
        paper_id=paper_id,
        file_id=file_id,
        file_hash=file_hash,
        extractor_fingerprint=pdf_ir.DEFAULT_EXTRACTOR_FINGERPRINT,
        pages=pages,
        extraction_warnings=("layout-structure-unclassified",),
    )


def test_canonical_ir_import_performs_no_runtime_io(tmp_path):
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    (stub_dir / "pdfplumber.py").write_text(
        "def open(*args, **kwargs):\n"
        "    raise AssertionError('pdfplumber.open called during import')\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(stub_dir), str(SERVICE_DIR)))

    result = subprocess.run(
        [sys.executable, "-c", "import pdf_ir"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_extractor_preserves_pages_normalizes_text_and_verifies_hash(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"fake-pdf")
    expected_hash = hashlib.sha256(b"fake-pdf").hexdigest()
    fake_pdf = _FakePdf([_FakePage("one\r\ntwo"), _FakePage(None)])
    monkeypatch.setattr(pdf_ir.pdfplumber, "open", lambda _: fake_pdf)

    document = pdf_ir.extract_pdf_document(
        path,
        paper_id="paper-1",
        file_id="paper-1-main",
        expected_file_hash=expected_hash,
        printed_page_labels={0: "1"},
    )

    assert document.file_hash == expected_hash
    assert [page.normalized_text for page in document.pages] == [
        "one\ntwo",
        "",
    ]
    assert document.pages[0].printed_page_label == "1"
    assert document.pages[1].extraction_warnings == ("empty-page-text",)
    assert document.extraction_warnings == ("layout-structure-unclassified",)

    with pytest.raises(pdf_ir.CanonicalIRError, match="file hash mismatch"):
        pdf_ir.extract_pdf_document(
            path,
            paper_id="paper-1",
            file_id="paper-1-main",
            expected_file_hash="b" * 64,
        )


def test_c0_adapter_matches_legacy_text_and_tracks_cross_page_spans():
    document = _document("A" * 500, "B" * 500, "C" * 500)
    joined = "\n".join(page.normalized_text for page in document.pages)

    chunks = pdf_ir.adapt_legacy_c0(document, is_main=True)

    assert [chunk.text for chunk in chunks] == pdf_baseline.chunk_text(joined)
    assert chunks[0].start_page == 0
    assert chunks[0].end_page == 1
    assert [span.pdf_page_index for span in chunks[0].source_spans] == [0, 1]
    assert chunks[0].previous_chunk_id is None
    assert chunks[0].next_chunk_id == chunks[1].chunk_id
    assert chunks[-1].previous_chunk_id == chunks[-2].chunk_id
    assert chunks[-1].next_chunk_id is None
    assert all(chunk.is_main and not chunk.is_si for chunk in chunks)
    assert all(
        "section-path-unavailable" in chunk.extraction_warnings
        for chunk in chunks
    )


def test_c0_adapter_preserves_final_reference_truncation_contract():
    document = _document(
        "body " * 200,
        "more " * 100 + "\nReferences\n" + "citation " * 200,
    )
    joined = "\n".join(page.normalized_text for page in document.pages)
    expected = pdf_baseline.chunk_text(
        pdf_baseline.truncate_final_references(joined)
    )

    chunks = pdf_ir.adapt_legacy_c0(document, is_main=True)

    assert [chunk.text for chunk in chunks] == expected
    assert all("citation" not in chunk.text for chunk in chunks)


def test_chunk_ids_are_stable_and_bind_file_and_chunker_fingerprints():
    document = _document("x" * 1700)

    first = pdf_ir.adapt_legacy_c0(document, is_main=True)
    second = pdf_ir.adapt_legacy_c0(document, is_main=True)
    changed_file = pdf_ir.adapt_legacy_c0(
        _document("x" * 1700, file_hash="b" * 64),
        is_main=True,
    )
    changed_config = pdf_ir.adapt_legacy_c0(
        document,
        is_main=True,
        chunk_size=700,
    )

    assert [chunk.chunk_id for chunk in first] == [
        chunk.chunk_id for chunk in second
    ]
    assert first[0].chunk_id != changed_file[0].chunk_id
    assert first[0].chunk_id != changed_config[0].chunk_id


def test_main_and_si_provenance_cannot_mix():
    main = pdf_ir.adapt_legacy_c0(_document("m" * 900), is_main=True)
    si = pdf_ir.adapt_legacy_c0(
        _document(
            "s" * 900,
            file_id="paper-1-si-1",
            file_hash="b" * 64,
        ),
        is_main=False,
    )

    assert {chunk.file_id for chunk in main} == {"paper-1-main"}
    assert {span.file_id for chunk in main for span in chunk.source_spans} == {
        "paper-1-main"
    }
    assert {chunk.file_id for chunk in si} == {"paper-1-si-1"}
    assert all(not chunk.is_main and chunk.is_si for chunk in si)


def test_evidence_mapping_requires_page_hash_and_full_coverage():
    document = _document("x" * 1000)
    chunks = pdf_ir.adapt_legacy_c0(
        document,
        is_main=True,
        chunk_size=500,
        chunk_step=400,
        min_chunk_len=0,
    )
    page = document.pages[0]
    evidence = pdf_ir.SourceSpan(
        file_id=document.file_id,
        pdf_page_index=0,
        char_start_in_normalized_page=450,
        char_end_in_normalized_page=550,
        page_text_hash=page.page_text_hash,
    )

    assert pdf_ir.map_evidence_span_to_chunks(chunks, evidence) == (
        chunks[0].chunk_id,
        chunks[1].chunk_id,
    )

    wrong_hash = pdf_ir.SourceSpan(
        file_id=document.file_id,
        pdf_page_index=0,
        char_start_in_normalized_page=450,
        char_end_in_normalized_page=550,
        page_text_hash="b" * 64,
    )
    with pytest.raises(pdf_ir.ProvenanceMismatchError):
        pdf_ir.map_evidence_span_to_chunks(chunks, wrong_hash)

    uncovered = pdf_ir.SourceSpan(
        file_id=document.file_id,
        pdf_page_index=0,
        char_start_in_normalized_page=950,
        char_end_in_normalized_page=1000,
        page_text_hash=page.page_text_hash,
    )
    with pytest.raises(pdf_ir.EvidenceMappingError, match="not fully covered"):
        pdf_ir.map_evidence_span_to_chunks(chunks[:2], uncovered)


def test_chunk_json_matches_committed_schema():
    chunk = pdf_ir.adapt_legacy_c0(_document("x" * 900), is_main=True)[0]
    schema = json.loads(
        (
            REPO_ROOT
            / "benchmarks"
            / "schemas"
            / "chunk-record.schema.json"
        ).read_text(encoding="utf-8")
    )

    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(chunk.to_dict())
