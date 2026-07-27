"""Pure legacy PDF extraction and fixed-character chunking.

This module is the behavior-preserving C0 seam used by both the production
CLI adapter and the benchmark plane. Importing it does not scan a vault, open
a PDF, read Zotero, initialize ChromaDB, or bind a collection.
"""

from __future__ import annotations

import re
from pathlib import Path

import pdfplumber


DEFAULT_CHUNK_SIZE = 800
DEFAULT_CHUNK_STEP = 700
DEFAULT_MIN_CHUNK_LEN = 100

REFERENCE_HEADING_PATTERN = re.compile(
    r"\n(References|REFERENCES|Bibliography|BIBLIOGRAPHY|"
    r"参考文献|Acknowledgements|ACKNOWLEDGEMENTS)\s*\n",
    re.IGNORECASE,
)


def extract_text_pdfplumber(pdf_path: str | Path) -> str:
    """Extract page text and preserve the legacy final-reference truncation."""
    with pdfplumber.open(pdf_path) as pdf:
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    final_heading = None
    for match in REFERENCE_HEADING_PATTERN.finditer(full_text):
        final_heading = match
    if final_heading:
        full_text = full_text[: final_heading.start()]
    return full_text


def chunk_text(
    text: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_step: int = DEFAULT_CHUNK_STEP,
    min_chunk_len: int = DEFAULT_MIN_CHUNK_LEN,
) -> list[str]:
    """Apply the legacy fixed-character sliding window.

    The strict ``len(chunk) > min_chunk_len`` boundary is intentional and
    matches the pre-seam production behavior.
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size must be greater than zero")
    if chunk_step <= 0:
        raise ValueError("chunk_step must be greater than zero")
    if min_chunk_len < 0:
        raise ValueError("min_chunk_len must be non-negative")

    chunks = []
    for offset in range(0, len(text), chunk_step):
        chunk = text[offset : offset + chunk_size]
        if len(chunk) > min_chunk_len:
            chunks.append(chunk)
    return chunks
