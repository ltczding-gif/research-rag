from __future__ import annotations

import pytest

from benchmarks.public_report import PublicReportError, sanitize_hits


def test_sanitize_hits_uses_an_explicit_public_allowlist():
    internal_hit = {
        "paper_id": "paper-001",
        "file_id": "paper-001-main",
        "pdf_page_index": 7,
        "evidence_id": "evidence-009",
        "pdf_path": r"C:\Users\Private\Zotero\storage\ABCD1234\paper.pdf",
        "vault_path": r"C:\Users\Private\research-note\secret.md",
        "document": "unlicensed verbatim source text",
        "query": "private query",
        "api_key": "should-never-appear",
        "score": 0.93,
    }

    public_hits = sanitize_hits([internal_hit])

    assert public_hits == [
        {
            "paper_id": "paper-001",
            "file_id": "paper-001-main",
            "pdf_page_index": 7,
            "evidence_id": "evidence-009",
        }
    ]
    serialized = repr(public_hits)
    assert "Private" not in serialized
    assert "unlicensed" not in serialized
    assert "api_key" not in serialized


@pytest.mark.parametrize(
    "hit",
    [
        {"paper_id": r"c:\private\paper", "file_id": "file-1"},
        {"paper_id": "paper-1", "file_id": "../private"},
        {"paper_id": "paper-1", "file_id": "file-1", "evidence_id": "BAD ID"},
        {"paper_id": "paper-1", "file_id": "file-1", "pdf_page_index": -1},
    ],
)
def test_sanitizer_fails_closed_on_unsafe_public_fields(hit):
    with pytest.raises(PublicReportError):
        sanitize_hits([hit])


def test_sanitizer_requires_stable_public_identifiers():
    with pytest.raises(PublicReportError, match="paper_id"):
        sanitize_hits([{"file_id": "file-1"}])
