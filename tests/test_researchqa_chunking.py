from __future__ import annotations

import hashlib

import pytest

from benchmarks.researchqa_chunking import (
    NOTE_CHUNKER_IDS,
    PDF_CHUNKER_IDS,
    chunk_note,
    chunk_pdf,
    note_chunk_pdf_backlinks,
    parse_source_citations,
)
from service.pdf_ir import (
    CanonicalDocument,
    DEFAULT_EXTRACTOR_FINGERPRINT,
    DocumentPage,
)


def _document(*page_texts: str) -> CanonicalDocument:
    pages = tuple(
        DocumentPage.create(
            paper_id="paper-1",
            file_id="paper-1-main",
            pdf_page_index=index,
            text=text,
        )
        for index, text in enumerate(page_texts)
    )
    return CanonicalDocument(
        paper_id="paper-1",
        file_id="paper-1-main",
        file_hash=hashlib.sha256(b"pdf").hexdigest(),
        extractor_fingerprint=DEFAULT_EXTRACTOR_FINGERPRINT,
        pages=pages,
    )


@pytest.mark.parametrize(
    ("config_id", "expected_lengths"),
    [
        ("pdf-fixed-400", [400, 400, 400, 340]),
        ("pdf-fixed-800", [800, 600]),
        ("pdf-fixed-1200", [1200, 300]),
    ],
)
def test_fixed_pdf_chunkers_have_approved_windows_and_stable_links(
    config_id,
    expected_lengths,
):
    document = _document("x" * 1300)

    first = chunk_pdf(document, config_id, is_main=True)
    second = chunk_pdf(document, config_id, is_main=True)

    assert first.status == "completed"
    assert [len(chunk.text) for chunk in first.chunks] == expected_lengths
    assert [chunk.chunk_id for chunk in first.chunks] == [
        chunk.chunk_id for chunk in second.chunks
    ]
    assert first.chunks[0].previous_chunk_id is None
    assert first.chunks[0].next_chunk_id == first.chunks[1].chunk_id
    assert first.chunks[-1].next_chunk_id is None
    assert all(chunk.source_spans for chunk in first.chunks)
    assert all(chunk.is_main and not chunk.is_si for chunk in first.chunks)


def test_fixed_minimum_is_strict_and_failure_is_explicit():
    failed = chunk_pdf(_document("x" * 100), "pdf-fixed-800", is_main=True)
    passed = chunk_pdf(_document("x" * 101), "pdf-fixed-800", is_main=True)

    assert failed.status == "failed"
    assert failed.failure_reason == "no-indexable-text"
    assert len(passed.chunks) == 1


def test_pdf_chunk_ids_bind_config_and_canonical_content():
    document = _document("x" * 900)
    changed_document = _document("y" * 900)

    fixed_400 = chunk_pdf(document, "pdf-fixed-400", is_main=True)
    fixed_800 = chunk_pdf(document, "pdf-fixed-800", is_main=True)
    changed = chunk_pdf(changed_document, "pdf-fixed-800", is_main=True)

    assert fixed_400.chunks[0].chunk_id != fixed_800.chunks[0].chunk_id
    assert fixed_800.chunks[0].chunk_id != changed.chunks[0].chunk_id


def test_page_aware_never_crosses_physical_pages():
    document = _document(
        "first paragraph\n\n" + "a" * 900,
        "second paragraph\n\n" + "b" * 900,
    )

    result = chunk_pdf(document, "pdf-page-aware", is_main=True)

    assert result.status == "completed"
    assert {chunk.start_page for chunk in result.chunks} == {0, 1}
    assert all(chunk.start_page == chunk.end_page for chunk in result.chunks)
    assert all(
        len({span.pdf_page_index for span in chunk.source_spans}) == 1
        for chunk in result.chunks
    )


def test_section_aware_detects_paths_and_fails_closed_without_headings():
    structured = _document(
        "1 Introduction\n"
        + "intro " * 120
        + "\n\n2 Methods\n"
        + "method " * 120
    )
    unstructured = _document("one undifferentiated paragraph")

    result = chunk_pdf(structured, "pdf-section-aware", is_main=True)
    failed = chunk_pdf(unstructured, "pdf-section-aware", is_main=True)

    assert result.status == "completed"
    assert [chunk.section_path for chunk in result.chunks] == [
        ("Introduction",),
        ("Methods",),
    ]
    assert failed.status == "failed"
    assert failed.failure_reason == "section-detection-failed"
    assert failed.chunks == ()


def test_section_aware_drops_punctuation_only_extraction_fragments():
    document = _document(
        "1 Methods\n\n"
        + "a" * 1200
        + "."
    )

    result = chunk_pdf(document, "pdf-section-aware", is_main=True)

    assert result.status == "completed"
    assert result.chunks
    assert all(
        any(character.isalnum() for character in chunk.text)
        for chunk in result.chunks
    )
    assert all(chunk.text.strip() != "." for chunk in result.chunks)


def test_structure_aware_preserves_detected_atomic_neighborhood():
    document = _document(
        "Introduction\n\n"
        "The preceding paragraph explains the result.\n\n"
        "Figure 1. Accuracy by condition.\n\n"
        "The following paragraph interprets the figure.\n\n"
        "Conclusion."
    )

    result = chunk_pdf(document, "pdf-structure-aware", is_main=True)
    failed = chunk_pdf(
        _document("Plain prose without any recognized structural marker."),
        "pdf-structure-aware",
        is_main=True,
    )

    assert result.status == "completed"
    assert any(
        "preceding paragraph" in chunk.text
        and "Figure 1." in chunk.text
        and "following paragraph" in chunk.text
        for chunk in result.chunks
    )
    assert failed.status == "failed"
    assert failed.failure_reason == "structure-detection-failed"


def test_structure_atomic_block_is_not_split_at_non_atomic_hard_max():
    document = _document(
        "Introduction\n\n"
        + "a" * 650
        + "\n\nFigure 1. Long caption.\n\n"
        + "b" * 650
    )

    result = chunk_pdf(document, "pdf-structure-aware", is_main=True)

    atomic = next(chunk for chunk in result.chunks if "Figure 1." in chunk.text)
    assert len(atomic.text) > 1200
    assert "a" * 650 in atomic.text
    assert "b" * 650 in atomic.text


def test_parent_child_returns_separate_parent_blocks_and_child_backlinks():
    document = _document(
        "Paragraph one. " * 70
        + "\n\n"
        + "Paragraph two. " * 70
        + "\n\n"
        + "Paragraph three. " * 70
    )

    result = chunk_pdf(document, "pdf-parent-child", is_main=True)

    assert result.status == "completed"
    assert result.parents
    parent_ids = {parent.chunk_id for parent in result.parents}
    assert all(parent.role == "parent" for parent in result.parents)
    assert all(chunk.role == "child" for chunk in result.chunks)
    assert all(chunk.parent_chunk_id in parent_ids for chunk in result.chunks)
    assert all(81 <= len(chunk.text) <= 400 for chunk in result.chunks)
    assert all(
        800 <= len(parent.text) <= 1600
        for parent in result.parents
    )


def test_parent_child_rebalances_uneven_paragraphs_to_approved_range():
    document = _document("a" * 700 + "\n\n" + "b" * 1000)

    result = chunk_pdf(document, "pdf-parent-child", is_main=True)

    assert len(result.parents) == 2
    assert all(800 <= len(parent.text) <= 1600 for parent in result.parents)
    assert "".join(parent.text for parent in result.parents) == document.pages[0].normalized_text


def test_all_seven_pdf_chunker_ids_are_public_and_unknown_is_rejected():
    assert len(PDF_CHUNKER_IDS) == 7
    with pytest.raises(ValueError, match="unknown PDF chunker"):
        chunk_pdf(_document("text"), "pdf-invented", is_main=True)


NOTE = """# Frozen note

## Evidence map
| Evidence ID | Source |
|---|---|
| E1 | result [Main p.2] |
| E2 | supplement [SI-01 p.3] |

## Findings
### C1：Primary bounded claim
The evidence chain uses E1 and E2. [Main p.2; SI-01 p.3]

### C2：Secondary claim
The evidence chain uses E1. [Main p.4]

## Reviewer verdict
| Claim | Verdict | Evidence | Alternative | Decisive evidence | Severity |
|---|---|---|---|---|---|
| C1 | partial | E1, E2 [Main p.2] | alternative | experiment | major |
| C2 | supported | E1 [Main p.4] | alternative | robustness | minor |

1. **C1/E1 concern** [Main p.2]. This is a surviving concern.

2. **Decisive evidence**
   - 对 C1：Run the discriminating experiment.
"""


def test_citation_parser_handles_pdf_and_native_si_coordinates():
    text = (
        '[Main p.5, 10-11; SI-01 p.3] '
        '[SI-02 para.14] '
        '[SI-03 table.2 rows.3-5 cols.A-D] '
        '[SI-04 sheet."Table S1" cells.A2:F18] '
        '[SI-05 rows.20-35 cols.model,score]'
    )
    citations = parse_source_citations(
        text,
        source_file_ids={
            "Main": "paper-main",
            "SI-01": "paper-si-1",
            "SI-02": "paper-si-2",
            "SI-03": "paper-si-3",
            "SI-04": "paper-si-4",
            "SI-05": "paper-si-5",
        },
    )

    assert [(item.page_start, item.page_end) for item in citations[:3]] == [
        (5, 5),
        (10, 11),
        (3, 3),
    ]
    assert [item.coordinate_type for item in citations[3:]] == [
        "docx_paragraph",
        "docx_table",
        "xlsx_cells",
        "csv_rows_columns",
    ]
    assert citations[0].is_benchmark_pdf
    assert not citations[2].is_benchmark_pdf


@pytest.mark.parametrize("config_id", NOTE_CHUNKER_IDS)
def test_all_note_chunkers_are_deterministic_and_keep_source_spans(config_id):
    first = chunk_note(
        NOTE,
        config_id,
        paper_id="paper-1",
        source_file_ids={"Main": "paper-1-main", "SI-01": "paper-1-si-1"},
    )
    second = chunk_note(
        NOTE,
        config_id,
        paper_id="paper-1",
        source_file_ids={"Main": "paper-1-main", "SI-01": "paper-1-si-1"},
    )

    assert first.status == "completed"
    assert first.chunks
    assert [chunk.chunk_id for chunk in first.chunks] == [
        chunk.chunk_id for chunk in second.chunks
    ]
    assert all(chunk.note_spans for chunk in first.chunks)
    assert all(
        chunk.note_sha256 == hashlib.sha256(NOTE.encode("utf-8")).hexdigest()
        for chunk in first.chunks
    )


def test_claim_evidence_and_reviewer_chunks_include_linked_records():
    claim_result = chunk_note(
        NOTE,
        "note-claim-evidence",
        paper_id="paper-1",
    )
    reviewer_result = chunk_note(
        NOTE,
        "note-reviewer-concern",
        paper_id="paper-1",
    )

    assert [chunk.claim_ids for chunk in claim_result.chunks] == [
        ("C1",),
        ("C2",),
    ]
    assert claim_result.chunks[0].evidence_ids == ("E1", "E2")
    assert "| E1 | result" in claim_result.chunks[0].text
    assert "| E2 | supplement" in claim_result.chunks[0].text
    assert len(reviewer_result.chunks) == 1
    concern = reviewer_result.chunks[0]
    assert concern.concern_id == "concern-c1"
    assert concern.claim_ids == ("C1",)
    assert concern.evidence_ids == ("E1", "E2")
    assert "surviving concern" in concern.text
    assert "Run the discriminating experiment" in concern.text


def test_note_main_citations_backlink_to_pdf_chunks_only():
    document = _document("a" * 900, "b" * 900, "c" * 900, "d" * 900)
    pdf_chunks = chunk_pdf(document, "pdf-page-aware", is_main=True).chunks
    note_chunk = chunk_note(
        NOTE,
        "note-whole",
        paper_id="paper-1",
        source_file_ids={"Main": "paper-1-main", "SI-01": "paper-1-si-1"},
    ).chunks[0]

    backlinks = note_chunk_pdf_backlinks(note_chunk, pdf_chunks)

    expected = {
        chunk.chunk_id
        for chunk in pdf_chunks
        if chunk.start_page + 1 in {2, 4}
    }
    assert set(backlinks) == expected
    assert all(
        pdf_chunk.is_main
        for pdf_chunk in pdf_chunks
        if pdf_chunk.chunk_id in backlinks
    )
