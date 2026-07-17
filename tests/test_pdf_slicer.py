"""Tests for `scanner/pdf_slicer.py`.

Two-page baseline + a 10-page baseline cover the two interesting
branches: passthrough (source ≤ n) and actual slicing (source > n).
"""

from __future__ import annotations

from pathlib import Path

import pytest


pypdf = pytest.importorskip("pypdf")
from pdf_slicer import slice_first_n_pages  # noqa: E402


def _write_blank_pdf(path: Path, page_count: int) -> Path:
    """Build a real PDF with `page_count` blank pages so tests aren't
    dependent on a checked-in fixture."""
    writer = pypdf.PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


def test_slice_first_n_pages_writes_n_pages(tmp_path):
    """A 10-page source sliced to n=3 produces a 3-page output."""
    src = _write_blank_pdf(tmp_path / "source.pdf", page_count=10)
    dst = tmp_path / "sliced.pdf"
    result = slice_first_n_pages(src, dst, n=3)
    assert result == dst.resolve()
    assert len(pypdf.PdfReader(str(result)).pages) == 3


def test_slice_first_n_pages_passthrough_when_source_shorter(tmp_path):
    """Source with fewer pages than n must NOT write a slice file —
    it returns the source path so callers send the whole short PDF."""
    src = _write_blank_pdf(tmp_path / "short.pdf", page_count=2)
    dst = tmp_path / "would_have_sliced.pdf"
    result = slice_first_n_pages(src, dst, n=3)
    assert result == src.resolve()
    assert not dst.exists()


def test_slice_first_n_pages_passthrough_when_source_equal(tmp_path):
    """Edge: source has exactly n pages — passthrough, no copy."""
    src = _write_blank_pdf(tmp_path / "exact.pdf", page_count=3)
    dst = tmp_path / "would_slice.pdf"
    result = slice_first_n_pages(src, dst, n=3)
    assert result == src.resolve()
    assert not dst.exists()


def test_slice_first_n_pages_rejects_invalid_n(tmp_path):
    src = _write_blank_pdf(tmp_path / "x.pdf", page_count=5)
    with pytest.raises(ValueError):
        slice_first_n_pages(src, tmp_path / "out.pdf", n=0)
    with pytest.raises(ValueError):
        slice_first_n_pages(src, tmp_path / "out.pdf", n=-1)


def test_slice_first_n_pages_creates_parent_directory(tmp_path):
    """If the output path's parent doesn't exist, the slicer should
    create it. Avoids a per-caller mkdir requirement."""
    src = _write_blank_pdf(tmp_path / "source.pdf", page_count=10)
    dst = tmp_path / "deeply" / "nested" / "out.pdf"
    result = slice_first_n_pages(src, dst, n=3)
    assert result == dst.resolve()
    assert dst.exists()
