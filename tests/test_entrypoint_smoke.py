"""
End-to-end smoke: invoke scanner/gemini_analyze_pdf.py as a real subprocess
(the way users and the batch scanner run it) with the zero-credential
subagent backend, and assert the documented exit-code contract.

This is the regression net for two release-blocking bugs found in the
2026-07-15 review:
  * the script losing its ``if __name__ == "__main__"`` guard — direct
    invocation became a silent no-op with exit 0, and
  * the argparse --backend fallback drifting away from the configured
    default backend (config.PROCESSOR_BACKEND).

Everything runs against a temp directory layout — no Zotero database,
no network, no API keys.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scanner" / "gemini_analyze_pdf.py"


def _make_blank_pdf(path: Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with open(path, "wb") as fh:
        writer.write(fh)


def _isolated_env(tmp_path: Path) -> dict:
    env = os.environ.copy()
    env.update(
        {
            "GEMINI_INCREMENTAL_ALIGNMENT_REPORT_ROOT": str(tmp_path / "reports"),
            "GEMINI_PROCESSED_HISTORY": str(tmp_path / "processed_history.txt"),
            "LOCALRAG_NOTES_DIR": str(tmp_path / "vault"),
            "LOCALRAG_HOME": str(tmp_path / "localrag"),
            "ZOTERO_DB_PATH": str(tmp_path / "no-such-zotero.sqlite"),
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        }
    )
    return env


def test_direct_invocation_emits_stage_a_manifest_and_exits_200(tmp_path):
    pdf = tmp_path / "paper.pdf"
    _make_blank_pdf(pdf)

    completed = subprocess.run(
        [sys.executable, str(SCRIPT), str(pdf), "--backend", "subagent"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_isolated_env(tmp_path),
        cwd=str(REPO_ROOT),
        timeout=120,
    )

    assert completed.returncode == 200, (
        "expected exit 200 (sub-agent manifest pending); got "
        f"{completed.returncode}.\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )

    manifests = list((tmp_path / "reports" / "runs").glob("*/manifest-profiler.json"))
    assert len(manifests) == 1, (
        f"expected exactly one Stage A manifest, found {manifests}.\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest.get("expected_output_path"), "manifest missing expected_output_path"


def test_backend_argparse_default_comes_from_config():
    """The CLI default must be wired to config.PROCESSOR_BACKEND (which reads
    LOCALRAG_PROCESSOR_BACKEND / .env), not a hardcoded literal — README's
    "a fresh clone runs with no credentials" promise depends on it."""
    import config
    import gemini_analyze_pdf
    import zotero_batch_scanner

    for mod in (gemini_analyze_pdf, zotero_batch_scanner):
        parser = mod.build_arg_parser()
        assert parser.get_default("backend") == config.PROCESSOR_BACKEND, (
            f"{mod.__name__}: --backend default drifted from config.PROCESSOR_BACKEND"
        )
