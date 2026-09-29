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


def test_build_commands_support_notes_only_and_explicit_removals():
    commands = build_indexes.build_commands(
        "/venv/python",
        allow_removals=True,
        notes_only=True,
    )

    assert commands == [
        [
            "/venv/python",
            str(build_indexes.REPO_ROOT / "service" / "build_notes_db.py"),
            "--allow-removals",
        ]
    ]


def test_run_builds_stops_on_first_failure():
    calls = []

    def runner(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 7)

    status = build_indexes.run_builds([["python", "notes.py"], ["python", "papers.py"]], runner)
    assert status == 7
    assert len(calls) == 1


def test_allow_removals_reaches_both_snapshot_builders():
    commands = build_indexes.build_commands("/venv/python", allow_removals=True, keyword_index=False)
    assert len(commands) == 2
    assert all(command[-1] == "--allow-removals" for command in commands)


def test_keyword_index_follows_the_papers_build_unless_disabled():
    commands = build_indexes.build_commands("/venv/python")
    assert [Path(c[1]).name for c in commands] == [
        "build_notes_db.py", "build_pdf_db.py", "build_lexical_index.py"]
    assert len(build_indexes.build_commands("/venv/python", keyword_index=False)) == 2
    assert len(build_indexes.build_commands("/venv/python", notes_only=True)) == 1


def test_keyword_index_failure_is_a_committed_warning():
    def runner(command, **kwargs):
        return subprocess.CompletedProcess(command, 1 if command[1].endswith("build_lexical_index.py") else 0)

    status = build_indexes.run_builds(build_indexes.build_commands("python"), runner)
    assert status == 3


def test_run_builds_runs_both_on_success():
    calls = []

    def runner(command, **kwargs):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0)

    status = build_indexes.run_builds([["python", "notes.py"], ["python", "papers.py"]], runner)
    assert status == 0
    assert len(calls) == 2
