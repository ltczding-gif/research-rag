"""
PDF slicing helper for the Stage A profiler optimization.

Stage A (Document Profiler) only needs the first few pages of a paper
to classify it — title, abstract, and the opening of the introduction
typically suffice. Sending the full PDF to the profiler wastes ~30k
input tokens per paper for what is effectively a classification task.

This module produces a small "profiler" PDF containing only the first
``n`` pages of a source document. The orchestrator decides which PDF
in a multi-PDF group is the primary one (index 0 by convention) and
slices only that; SI files and chapter splits are not classified.

Pure helper: takes a source path, writes a sliced copy at an explicit
output path, and returns the path that should be sent to the model.
For sources already ≤ n pages, returns the source unchanged so callers
don't have to special-case short inputs.
"""

from __future__ import annotations

from pathlib import Path


def slice_first_n_pages(source_pdf, output_path, n: int) -> Path:
    """Write ``source_pdf``'s first ``n`` pages to ``output_path``.

    If the source has ≤ ``n`` pages, returns the resolved source path
    unchanged (no slice file is created). Otherwise writes a new PDF
    containing exactly the first ``n`` pages and returns ``output_path``.

    Args:
        source_pdf: Path to the source PDF.
        output_path: Where to write the sliced PDF if a slice is needed.
        n: Number of leading pages to keep. Must be ≥ 1.

    Returns:
        The resolved Path the caller should send downstream — either
        ``output_path`` (when a slice was written) or the source path
        (when the source was short enough to be a passthrough).

    Raises:
        ValueError: if ``n < 1``.
        FileNotFoundError: if ``source_pdf`` doesn't exist.
        RuntimeError: if pypdf isn't installed.
    """
    if n < 1:
        raise ValueError(f"n must be >= 1, got {n}")

    source_pdf = Path(source_pdf).resolve()
    output_path = Path(output_path).resolve()
    if not source_pdf.exists():
        raise FileNotFoundError(f"source PDF not found: {source_pdf}")

    try:
        from pypdf import PdfReader, PdfWriter
    except ImportError as e:
        raise RuntimeError(
            "pdf_slicer requires `pypdf`. Install with: pip install pypdf"
        ) from e

    reader = PdfReader(str(source_pdf))
    page_count = len(reader.pages)
    if page_count <= n:
        # Passthrough: the source is already short enough. Don't write a
        # redundant copy. Callers can detect passthrough by checking
        # whether the returned path equals their requested output_path.
        return source_pdf

    writer = PdfWriter()
    for page_idx in range(n):
        writer.add_page(reader.pages[page_idx])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "wb") as fh:
        writer.write(fh)
    return output_path


__all__ = ["slice_first_n_pages"]
