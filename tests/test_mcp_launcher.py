"""Keep virtual-environment identity when its Python is a symlink."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock


def _launcher():
    source = Path(__file__).resolve().parents[1] / "scripts" / "run_mcp_server.py"
    spec = importlib.util.spec_from_file_location("launcher_under_test", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_venv_symlink_to_base_python_still_reexecutes(monkeypatch, tmp_path):
    launcher = _launcher()
    base = tmp_path / "base" / "python"
    candidate = tmp_path / ".venv" / "bin" / "python"
    candidate.parent.mkdir(parents=True)
    candidate.touch()
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        # POSIX venv/bin/python commonly resolves to the same base executable.
        if path in (candidate, base):
            return base
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(launcher.sys, "executable", str(base))
    monkeypatch.setattr(launcher, "_VENV_PYTHONS", [candidate])
    run = Mock(return_value=SimpleNamespace(returncode=23))
    in_process = Mock()
    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.runpy, "run_path", in_process)

    assert launcher.main() == 23
    run.assert_called_once_with(
        [str(candidate), str(launcher.SERVER)], cwd=str(launcher.REPO_ROOT)
    )
    in_process.assert_not_called()


def test_current_venv_interpreter_runs_server_in_process(monkeypatch, tmp_path):
    launcher = _launcher()
    candidate = tmp_path / ".venv" / "bin" / "python"
    candidate.parent.mkdir(parents=True)
    candidate.touch()
    monkeypatch.setattr(launcher.sys, "executable", str(candidate))
    monkeypatch.setattr(launcher.sys, "argv", ["launcher"])
    monkeypatch.setattr(launcher, "_VENV_PYTHONS", [candidate])
    run = Mock()
    in_process = Mock()
    monkeypatch.setattr(launcher.subprocess, "run", run)
    monkeypatch.setattr(launcher.runpy, "run_path", in_process)

    assert launcher.main() == 0
    run.assert_not_called()
    in_process.assert_called_once_with(str(launcher.SERVER), run_name="__main__")
