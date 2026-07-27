from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO_ROOT / "service"
sys.path.insert(0, str(SERVICE_DIR))

import pdf_baseline  # noqa: E402


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


def test_pure_baseline_import_performs_no_runtime_io(tmp_path):
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
        [sys.executable, "-c", "import pdf_baseline"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == ""


def test_build_pdf_db_import_does_not_scan_notes(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    (notes_dir / "would-have-been-scanned.md").write_text(
        "---\npdf_0_path: missing.pdf\n---\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SERVICE_DIR)
    env["LOCALRAG_NOTES_DIR"] = str(notes_dir)

    result = subprocess.run(
        [sys.executable, "-c", "import build_pdf_db"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "[INIT]" not in result.stdout
    assert "would-have-been-scanned.md" not in result.stdout


def test_legacy_extractor_joins_pages_and_truncates_at_last_references(monkeypatch):
    fake_pdf = _FakePdf(
        [
            _FakePage("Introduction"),
            _FakePage(None),
            _FakePage("Body\nReferences\nfirst list"),
            _FakePage("Appendix\nREFERENCES\nfinal list"),
        ]
    )
    monkeypatch.setattr(pdf_baseline.pdfplumber, "open", lambda _: fake_pdf)

    extracted = pdf_baseline.extract_text_pdfplumber("paper.pdf")

    assert extracted == "Introduction\n\nBody\nReferences\nfirst list\nAppendix"


def test_fixed_char_chunker_matches_legacy_window_contract():
    text = "".join(str(index % 10) for index in range(1700))

    chunks = pdf_baseline.chunk_text(text)

    assert chunks == [text[0:800], text[700:1500], text[1400:1700]]


def test_fixed_char_chunker_keeps_legacy_strict_minimum_boundary():
    assert pdf_baseline.chunk_text("x" * 100) == []
    assert pdf_baseline.chunk_text("x" * 101) == ["x" * 101]


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"chunk_size": 0}, "chunk_size"),
        ({"chunk_step": 0}, "chunk_step"),
        ({"min_chunk_len": -1}, "min_chunk_len"),
    ],
)
def test_fixed_char_chunker_rejects_invalid_configuration(kwargs, message):
    with pytest.raises(ValueError, match=message):
        pdf_baseline.chunk_text("content", **kwargs)
