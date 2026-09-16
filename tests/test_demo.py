from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import demo  # noqa: E402
import verify_mcp_e2e  # noqa: E402


def test_demo_environment_is_isolated(tmp_path):
    env = demo.demo_environment(tmp_path)
    assert Path(env["LOCALRAG_HOME"]).parent == tmp_path
    assert Path(env["LOCALRAG_NOTES_DIR"]).parent == tmp_path
    assert Path(env["LOCALRAG_CHROMA_PATH"]).is_relative_to(tmp_path)
    assert Path(env["LOCALRAG_PDF_LEDGER"]).is_relative_to(tmp_path)
    assert Path(env["LOCALRAG_NOTES_LEDGER"]).is_relative_to(tmp_path)
    assert Path(env["LOCALRAG_TEXTBOOK_LEDGER"]).is_relative_to(tmp_path)
    assert env["LOCALRAG_NOTES_COLLECTION"] == "notes"
    assert env["LOCALRAG_PAPERS_COLLECTION"] == "papers"
    assert env["LOCALRAG_EMBED_PROVIDER"] == "fastembed"
    assert env["LOCALRAG_FASTEMBED_MODEL"] == (
        "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    )
    assert env["LOCALRAG_E2E_SPAWN_PYTHON"] == sys.executable


def test_demo_builds_the_synthetic_corpus(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(demo.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["demo.py"])

    assert demo.main() == 0
    assert captured["args"][-1] == "--build"


def test_demo_can_use_current_python_for_direct_server(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(demo.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", ["demo.py", "--direct-server"])

    assert demo.main() == 0
    assert captured["args"][-2:] == ["--build", "--direct-server"]


def test_demo_corpus_declares_synthetic_identity():
    assert verify_mcp_e2e._CORPUS
    for note in verify_mcp_e2e._CORPUS.values():
        assert "synthetic_fixture: true" in note
        assert "zotero_parent_key: SYNTH-" in note
