"""
Canonical combined-hash algorithms for PDF groups.

All scanner files should import from this module. The two variants (stable
and legacy) come from a 2026-03 transition: notes generated before that date
used `legacy_combined_hash`; everything since uses `stable_combined_hash`.
Both are still recognized for ledger lookups so old notes don't get
re-processed.

Service-side scripts (`service/build_pdf_db.py`) duplicate the logic locally
with a "KEEP IN SYNC" comment, because `service/` and `scanner/` are not a
shared package — they're meant to be deployable independently. Keep both
copies in sync if you change the algorithm here.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Iterable


# Per-process memo for get_file_hash, keyed by (abspath, size, mtime_ns).
# A single batch pass hashes the same PDF up to 6 times (group signature,
# prefilter stable+legacy, subagent run-dir key, then again in the worker) —
# at 800 papers x 20 MB that is ~80 GB of redundant hash I/O. The
# size+mtime key invalidates the memo whenever the file changes.
_file_hash_cache: dict[tuple[str, int, int], str] = {}


def get_file_hash(filepath: str | Path, chunk_size: int = 1024 * 1024) -> str:
    """Streamed SHA-256 of a single file (memoized per process)."""
    abs_path = os.path.abspath(str(filepath))
    try:
        stat = os.stat(abs_path)
        cache_key = (abs_path, stat.st_size, stat.st_mtime_ns)
    except OSError:
        cache_key = None
    if cache_key is not None and cache_key in _file_hash_cache:
        return _file_hash_cache[cache_key]

    h = hashlib.sha256()
    with open(abs_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if cache_key is not None:
        _file_hash_cache[cache_key] = digest
    return digest


def normalize_pdf_group_paths(paths: Iterable[str | Path]) -> list[str]:
    """Dedup by case-folded absolute path; sort for determinism.

    Returns a list of absolute path strings, deduplicated case-insensitively
    (Windows-friendly), sorted lexicographically.
    """
    deduped: dict[str, str] = {}
    for path in paths:
        abs_path = os.path.abspath(str(path))
        deduped.setdefault(abs_path.casefold(), abs_path)
    return sorted(deduped.values(), key=lambda v: v.casefold())


def stable_combined_hash(paths: Iterable[str | Path]) -> str:
    """Order-independent SHA-256 combining one or more PDFs.

    Algorithm:
      1. For each path, compute SHA-256 of file bytes.
      2. Sort the resulting hex digests lexicographically.
      3. Concatenate them as UTF-8 strings into a single SHA-256.

    The sort step makes the result independent of input order — passing
    [main, SI] or [SI, main] yields the same hash. This is the canonical
    algorithm used everywhere from 2026-03 onward.
    """
    h = hashlib.sha256()
    file_hashes = sorted(get_file_hash(p) for p in normalize_pdf_group_paths(paths))
    for fh in file_hashes:
        h.update(fh.encode("utf-8"))
    return h.hexdigest()


def legacy_combined_hash(paths: Iterable[str | Path]) -> str:
    """Path-order SHA-256 combining one or more PDFs.

    Pre-2026-03 algorithm: file hashes appended in path-sorted (not
    hash-sorted) order. Different from `stable_combined_hash` for groups
    where the path-sorted and hash-sorted orders differ. Kept so old
    notes don't get re-processed after the algorithm changed.
    """
    h = hashlib.sha256()
    for path in normalize_pdf_group_paths(paths):
        h.update(get_file_hash(path).encode("utf-8"))
    return h.hexdigest()


def combined_hash_variants(paths: Iterable[str | Path]) -> dict:
    """Return both hash variants plus the accepted-hashes set for lookups.

    `accepted_hashes` is what to check a ledger against: it includes the
    stable hash always, plus the legacy hash when the two differ. Most
    groups produce the same value for both — the legacy entry only adds
    value for the few groups where path-sort and hash-sort disagree.
    """
    stable = stable_combined_hash(paths)
    legacy = legacy_combined_hash(paths)
    accepted = [stable]
    if legacy != stable:
        accepted.append(legacy)
    return {
        "combined_hash": stable,           # the canonical one
        "stable_combined_hash": stable,    # explicit alias
        "legacy_combined_hash": legacy,
        "accepted_hashes": accepted,
    }


def find_processed_hash_match(
    processed_hashes: set[str],
    combined_hash: str,
    legacy_combined_hash: str | None = None,
) -> str | None:
    """Return the matched hash if either variant is in `processed_hashes`."""
    for candidate in (combined_hash, legacy_combined_hash):
        if candidate and candidate in processed_hashes:
            return candidate
    return None


__all__ = [
    "get_file_hash",
    "normalize_pdf_group_paths",
    "stable_combined_hash",
    "legacy_combined_hash",
    "combined_hash_variants",
    "find_processed_hash_match",
]
