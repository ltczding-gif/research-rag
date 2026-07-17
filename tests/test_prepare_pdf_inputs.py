"""Orchestration test for `gemini_analyze_pdf.prepare_pdf_inputs_for_vertex`.

Specifically covers the new profiler-set return fields. Verifies:
  - 10-page PDF → profiler set is a 3-page slice file under work_dir/profiler/
  - 2-page PDF (short)  → profiler set passes the source path through
  - Multi-PDF group     → profiler set has exactly one entry (the main slice)
                            and SI files are listed in siblings_excluded
  - profiler_first_n_pages=None disables the optimization entirely
"""

from __future__ import annotations

from pathlib import Path

import pytest


pypdf = pytest.importorskip("pypdf")


def _write_blank_pdf(path: Path, page_count: int) -> Path:
    writer = pypdf.PdfWriter()
    for _ in range(page_count):
        writer.add_blank_page(width=72, height=72)
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


@pytest.fixture(scope="module")
def orch():
    """Lazy import — pulls in gemini_analyze_pdf only when this test runs."""
    import gemini_analyze_pdf as module

    return module


def test_prepare_returns_sliced_profiler_for_long_primary(tmp_path, orch):
    src = _write_blank_pdf(tmp_path / "paper.pdf", page_count=10)
    prepared = orch.prepare_pdf_inputs_for_vertex(
        pdf_paths=[str(src)],
        work_dir=tmp_path / "work",
    )
    assert prepared["profiler_pdf_paths"] is not None
    assert len(prepared["profiler_pdf_paths"]) == 1

    profiler_path = Path(prepared["profiler_pdf_paths"][0])
    assert profiler_path.exists()
    assert len(pypdf.PdfReader(str(profiler_path)).pages) == 3

    manifest = prepared["profiler_manifest"]
    assert manifest["primary_total_pages"] == 10
    assert manifest["profiler_pages_sent"] == 3
    assert manifest["transformation"] == "sliced"


def test_prepare_passthrough_for_short_primary(tmp_path, orch):
    src = _write_blank_pdf(tmp_path / "short.pdf", page_count=2)
    prepared = orch.prepare_pdf_inputs_for_vertex(
        pdf_paths=[str(src)],
        work_dir=tmp_path / "work",
    )
    assert prepared["profiler_pdf_paths"] is not None
    profiler_path = Path(prepared["profiler_pdf_paths"][0]).resolve()
    assert profiler_path == src.resolve()

    manifest = prepared["profiler_manifest"]
    assert manifest["transformation"] == "passthrough"
    assert manifest["profiler_pages_sent"] == 2


def test_prepare_only_slices_primary_in_multi_pdf_group(tmp_path, orch):
    main_pdf = _write_blank_pdf(tmp_path / "main.pdf", page_count=10)
    si_pdf = _write_blank_pdf(tmp_path / "si.pdf", page_count=20)
    prepared = orch.prepare_pdf_inputs_for_vertex(
        pdf_paths=[str(main_pdf), str(si_pdf)],
        work_dir=tmp_path / "work",
    )
    # profiler set has only 1 entry — the main PDF's 3-page slice
    assert len(prepared["profiler_pdf_paths"]) == 1
    manifest = prepared["profiler_manifest"]
    assert manifest["primary_pdf_path"].endswith("main.pdf")
    # SI must be recorded as excluded so the audit artifact tells you
    # what Stage A did NOT see.
    assert any("si.pdf" in path for path in manifest["siblings_excluded"])


def test_prepare_disabled_returns_none(tmp_path, orch):
    src = _write_blank_pdf(tmp_path / "x.pdf", page_count=10)
    prepared = orch.prepare_pdf_inputs_for_vertex(
        pdf_paths=[str(src)],
        work_dir=tmp_path / "work",
        profiler_first_n_pages=None,
    )
    assert prepared["profiler_pdf_paths"] is None
    assert prepared["profiler_manifest"] is None
