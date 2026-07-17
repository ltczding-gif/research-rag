"""Tests for `service/embedding_client.py:detect_dim_mismatch`.

Uses a function-scoped fixture to isolate `service/config.py` from
`scanner/config.py` in pytest's shared `sys.modules`. Both files are
named `config`; without this isolation, whichever test loads first
"wins" and the other side breaks.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def embedding_client():
    """Load service/embedding_client with service/config swapped in.

    Restores the prior `config` and `embedding_client` entries in
    sys.modules afterward so other test files (which use scanner/config)
    are unaffected.
    """
    repo_root = Path(__file__).resolve().parent.parent
    service_dir = repo_root / "service"
    sys.path.insert(0, str(service_dir))
    saved_config = sys.modules.pop("config", None)
    saved_client = sys.modules.pop("embedding_client", None)
    try:
        import embedding_client as _ec
        yield _ec
    finally:
        try:
            sys.path.remove(str(service_dir))
        except ValueError:
            pass
        sys.modules.pop("embedding_client", None)
        sys.modules.pop("config", None)
        if saved_client is not None:
            sys.modules["embedding_client"] = saved_client
        if saved_config is not None:
            sys.modules["config"] = saved_config


def _fake_collection(*, count: int, stored_dim: int | None):
    col = MagicMock()
    col.count.return_value = count
    if stored_dim is None:
        col.peek.return_value = {"embeddings": []}
    else:
        col.peek.return_value = {"embeddings": [[0.0] * stored_dim]}
    return col


def test_detect_dim_mismatch_skipped_for_empty_collection(embedding_client):
    col = _fake_collection(count=0, stored_dim=None)
    mismatch, msg = embedding_client.detect_dim_mismatch(col)
    assert mismatch is False
    assert "empty" in msg


def test_detect_dim_mismatch_no_warning_when_dims_match(embedding_client, monkeypatch):
    monkeypatch.setattr(embedding_client, "get_embedding", lambda text: [0.0] * 768)
    col = _fake_collection(count=10, stored_dim=768)
    mismatch, msg = embedding_client.detect_dim_mismatch(col)
    assert mismatch is False
    assert "768" in msg


def test_detect_dim_mismatch_flags_dim_change(embedding_client, monkeypatch):
    monkeypatch.setattr(embedding_client, "get_embedding", lambda text: [0.0] * 1024)
    col = _fake_collection(count=10, stored_dim=2560)
    mismatch, msg = embedding_client.detect_dim_mismatch(col)
    assert mismatch is True
    assert "2560" in msg
    assert "1024" in msg
    assert "rm -rf" in msg
    assert "build_notes_db.py" in msg
    assert "build_pdf_db.py" in msg


def test_detect_dim_mismatch_handles_probe_failure(embedding_client, monkeypatch):
    def _bad_embed(text):
        raise RuntimeError("probe failed: API quota exceeded")

    monkeypatch.setattr(embedding_client, "get_embedding", _bad_embed)
    col = _fake_collection(count=10, stored_dim=1024)
    mismatch, msg = embedding_client.detect_dim_mismatch(col)
    assert mismatch is False
    assert "probe failed" in msg


def test_detect_dim_mismatch_handles_collection_count_failure(embedding_client):
    col = MagicMock()
    col.count.side_effect = RuntimeError("chroma backend crashed")
    mismatch, msg = embedding_client.detect_dim_mismatch(col)
    assert mismatch is False
    assert "count() failed" in msg


def test_detect_dim_mismatch_handles_peek_failure(embedding_client, monkeypatch):
    monkeypatch.setattr(embedding_client, "get_embedding", lambda text: [0.0] * 1024)
    col = MagicMock()
    col.count.return_value = 10
    col.peek.side_effect = RuntimeError("could not read embeddings")
    mismatch, msg = embedding_client.detect_dim_mismatch(col)
    assert mismatch is False
    assert "could not read" in msg
