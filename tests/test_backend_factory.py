"""Smoke tests for scanner/backends/__init__.py factories."""

from __future__ import annotations

import pytest

from backends import BACKEND_NAMES, ProcessorBackend, make_backend


def test_make_backend_subagent_no_sdk_required():
    """The subagent backend has no external SDK dependency, so this
    should always succeed without any optional install."""
    backend = make_backend("subagent")
    assert isinstance(backend, ProcessorBackend)
    assert backend.name == "subagent"


def test_make_backend_unknown_raises():
    with pytest.raises(ValueError, match="Unknown processor backend"):
        make_backend("does-not-exist")


def test_make_backend_normalizes_underscore_to_hyphen():
    """Internal alias normalisation: 'gemini_api' should be accepted same as
    'gemini-api' (both pass through to the same backend at the dispatch level)."""
    # We can't actually instantiate without GEMINI_API_KEY, but we can verify
    # the dispatch routes to the same exception (missing api_key) rather than
    # 'Unknown backend'.
    with pytest.raises((ValueError, RuntimeError, TypeError)) as exc_info:
        make_backend("gemini_api")
    # Should reach the gemini-api branch (which then complains about missing key
    # or wrong kwargs), not the 'Unknown processor backend' ValueError.
    assert "Unknown processor backend" not in str(exc_info.value)


def test_backend_names_canonical_set():
    """BACKEND_NAMES is the source of truth for argparse `choices=`. If a
    new backend is added it must appear here."""
    assert "vertex" in BACKEND_NAMES
    assert "gemini-api" in BACKEND_NAMES
    assert "anthropic" in BACKEND_NAMES
    assert "openai" in BACKEND_NAMES
    assert "subagent" in BACKEND_NAMES
    # Underscore-aliased forms should NOT be in the canonical list.
    assert "gemini_api" not in BACKEND_NAMES
    assert "openai_api" not in BACKEND_NAMES
