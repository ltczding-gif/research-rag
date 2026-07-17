from __future__ import annotations

import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import demo  # noqa: E402


def test_demo_environment_is_isolated(tmp_path):
    env = demo.demo_environment(tmp_path)
    assert Path(env["LOCALRAG_HOME"]).parent == tmp_path
    assert Path(env["LOCALRAG_NOTES_DIR"]).parent == tmp_path
    assert env["LOCALRAG_EMBED_PROVIDER"] == "fastembed"
    assert env["LOCALRAG_E2E_SPAWN_PYTHON"] == sys.executable


def test_demo_builds_the_synthetic_corpus(monkeypatch):
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(demo.subprocess, "run", fake_run)

    assert demo.main() == 0
    assert captured["args"][-1] == "--build"
