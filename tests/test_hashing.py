"""Smoke tests for scanner/_hashing.py."""

from __future__ import annotations

from pathlib import Path

import pytest

from _hashing import (
    combined_hash_variants,
    find_processed_hash_match,
    get_file_hash,
    legacy_combined_hash,
    normalize_pdf_group_paths,
    stable_combined_hash,
)


@pytest.fixture
def two_files(tmp_path: Path) -> tuple[Path, Path]:
    """Two distinct files with deterministic content."""
    main = tmp_path / "main.pdf"
    si = tmp_path / "si.pdf"
    main.write_bytes(b"hello world")
    si.write_bytes(b"supplementary information")
    return main, si


def test_get_file_hash_is_deterministic(two_files):
    main, _ = two_files
    assert get_file_hash(main) == get_file_hash(main)


def test_normalize_dedups_case_insensitive(tmp_path):
    p = tmp_path / "X.pdf"
    p.write_bytes(b"x")
    out = normalize_pdf_group_paths([str(p), str(p).upper(), str(p)])
    assert len(out) == 1


def test_stable_combined_hash_is_order_independent(two_files):
    a, b = two_files
    h1 = stable_combined_hash([a, b])
    h2 = stable_combined_hash([b, a])
    assert h1 == h2


def test_legacy_combined_hash_is_input_order_invariant(tmp_path, two_files):
    """legacy variant internally sorts paths via normalize_pdf_group_paths,
    so passing [a, b] and [b, a] yields the same hash. The "legacy" name
    refers to the algorithm being from before stable_combined_hash existed —
    not to it being order-dependent."""
    a, b = two_files
    h1 = legacy_combined_hash([a, b])
    h2 = legacy_combined_hash([b, a])
    assert h1 == h2
    assert len(h1) == 64 and all(c in "0123456789abcdef" for c in h1)


def test_combined_hash_variants_shape(two_files):
    a, b = two_files
    v = combined_hash_variants([a, b])
    assert set(v.keys()) >= {
        "combined_hash",
        "stable_combined_hash",
        "legacy_combined_hash",
        "accepted_hashes",
    }
    assert v["combined_hash"] == v["stable_combined_hash"]
    assert v["combined_hash"] in v["accepted_hashes"]


def test_find_processed_hash_match_prefers_stable():
    stable = "a" * 64
    legacy = "b" * 64
    processed = {stable, legacy}
    assert find_processed_hash_match(processed, stable, legacy) == stable
    # When stable is missing, falls back to legacy
    assert find_processed_hash_match({legacy}, stable, legacy) == legacy
    # Neither in the set: None
    assert find_processed_hash_match({"c" * 64}, stable, legacy) is None


def test_single_file_group(tmp_path):
    p = tmp_path / "only.pdf"
    p.write_bytes(b"single")
    h_stable = stable_combined_hash([p])
    h_legacy = legacy_combined_hash([p])
    # For a single file, both algorithms produce the same result
    # (sort([h]) == [h] regardless of variant).
    assert h_stable == h_legacy
