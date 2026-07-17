from __future__ import annotations

import subprocess
import sys
from pathlib import Path


SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))
import build_indexes  # noqa: E402


def test_build_commands_use_current_interpreter_and_optional_rebuild():
    commands = build_indexes.build_commands("/venv/python", rebuild_papers=True)
    assert commands[0][0] == "/venv/python"
    assert Path(commands[0][-1]).name == "build_notes_db.py"
    assert Path(commands[1][-2]).name == "build_pdf_db.py"
    assert commands[1][-1] == "--rebuild"


def test_run_builds_stops_on_first_failure():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 7)

    status = build_indexes.run_builds([["python", "notes.py"], ["python", "papers.py"]], runner)
    assert status == 7
    assert len(calls) == 1


def test_run_builds_runs_both_on_success():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    status = build_indexes.run_builds([["python", "notes.py"], ["python", "papers.py"]], runner)
    assert status == 0
    assert len(calls) == 2
