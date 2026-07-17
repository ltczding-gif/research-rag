"""Inline `.env` loader in scanner/config.py and service/config.py.

The README promises a fresh clone runs with no credentials by virtue of
.env defaulting to `LOCALRAG_PROCESSOR_BACKEND=subagent`. That promise
is only true if .env is actually loaded before argparse reads
$LOCALRAG_PROCESSOR_BACKEND. These tests pin that behavior so the
no-API-key happy path stays honest across hosts.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest


def _make_dotenv(tmp_path: Path, content: str) -> Path:
    repo = tmp_path / "fake_repo"
    repo.mkdir()
    (repo / ".env").write_text(content, encoding="utf-8")
    return repo


def test_hydrate_loads_simple_keys(tmp_path, monkeypatch):
    from config import _hydrate_env_from_dotenv

    monkeypatch.delenv("FAKE_KEY_1", raising=False)
    monkeypatch.delenv("FAKE_KEY_2", raising=False)
    repo = _make_dotenv(
        tmp_path,
        "FAKE_KEY_1=hello\nFAKE_KEY_2=world\n",
    )

    _hydrate_env_from_dotenv(repo / ".env")

    assert os.environ["FAKE_KEY_1"] == "hello"
    assert os.environ["FAKE_KEY_2"] == "world"


def test_hydrate_strips_quotes(tmp_path, monkeypatch):
    from config import _hydrate_env_from_dotenv

    monkeypatch.delenv("FAKE_KEY_QUOTED", raising=False)
    monkeypatch.delenv("FAKE_KEY_SINGLE", raising=False)
    repo = _make_dotenv(
        tmp_path,
        'FAKE_KEY_QUOTED="value with spaces"\n'
        "FAKE_KEY_SINGLE='single quoted'\n",
    )

    _hydrate_env_from_dotenv(repo / ".env")

    assert os.environ["FAKE_KEY_QUOTED"] == "value with spaces"
    assert os.environ["FAKE_KEY_SINGLE"] == "single quoted"


def test_hydrate_skips_comments_and_blanks(tmp_path, monkeypatch):
    from config import _hydrate_env_from_dotenv

    monkeypatch.delenv("FAKE_KEY_REAL", raising=False)
    repo = _make_dotenv(
        tmp_path,
        "# this is a comment\n\n   \n# another comment\nFAKE_KEY_REAL=yes\n",
    )

    _hydrate_env_from_dotenv(repo / ".env")

    assert os.environ["FAKE_KEY_REAL"] == "yes"


def test_hydrate_does_not_clobber_existing_env(tmp_path, monkeypatch):
    """Shell export must always win over .env. Otherwise CI / containers
    can't override config without touching files."""
    from config import _hydrate_env_from_dotenv

    monkeypatch.setenv("FAKE_KEY_OVERRIDE", "from-shell")
    repo = _make_dotenv(tmp_path, "FAKE_KEY_OVERRIDE=from-dotenv\n")

    _hydrate_env_from_dotenv(repo / ".env")

    assert os.environ["FAKE_KEY_OVERRIDE"] == "from-shell"


def test_hydrate_handles_inline_comments_on_unquoted_values(tmp_path, monkeypatch):
    from config import _hydrate_env_from_dotenv

    monkeypatch.delenv("FAKE_KEY_INLINE", raising=False)
    repo = _make_dotenv(tmp_path, "FAKE_KEY_INLINE=plainvalue # comment\n")

    _hydrate_env_from_dotenv(repo / ".env")

    assert os.environ["FAKE_KEY_INLINE"] == "plainvalue"


def test_hydrate_silently_ignores_missing_file(tmp_path):
    from config import _hydrate_env_from_dotenv

    # Should not raise.
    _hydrate_env_from_dotenv(tmp_path / "nonexistent.env")


def test_hydrate_ignores_malformed_lines(tmp_path, monkeypatch):
    """Lines without `=` are skipped, not crashed on."""
    from config import _hydrate_env_from_dotenv

    monkeypatch.delenv("FAKE_KEY_GOOD", raising=False)
    repo = _make_dotenv(
        tmp_path,
        "this line has no equals sign\nFAKE_KEY_GOOD=ok\nanother bad line\n",
    )

    _hydrate_env_from_dotenv(repo / ".env")

    assert os.environ["FAKE_KEY_GOOD"] == "ok"
