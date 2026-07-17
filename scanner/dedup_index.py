"""
Unified dedup index for the Zotero → Gemini note pipeline.

Hides the "ledger + vault scan" combo behind a single object with three
operations:

  DedupIndex.build(history_path=..., vault_root=...)
      → DedupIndex   (reads ledger; scans vault for combined_hash and
                      zotero_parent_key from frontmatter)

  index.lookup(combined_hash=..., legacy_combined_hash=..., zotero_parent_key=...)
      → (matched_hash, note_path_or_None) | None

  index.append(combined_hash)
      → None         (atomic tmp+os.replace; idempotent)

Why this exists
---------------
Before this module, three places re-implemented "is this paper already
processed?" with subtly different fallback behavior:

- `gemini_analyze_pdf.py:main` only checked the flat-file ledger; the
  live-vault recovery path was bypassed in single-paper invocations
  unless an external `--note-index-file` was passed.
- `zotero_batch_scanner.py:prefilter_pdf_groups` checked both layers
  but built the vault index inline.
- `verify_and_clean.py` reimplemented the hash from scratch.

Centralizing this in `DedupIndex` gives F1 (single-paper auto-heal),
F4 (atomic ledger writes), and a stable shape that future hash
algorithm changes can extend without re-touching every call site.

Vault scan semantics match `gemini_analyze_pdf._iter_live_vault_note_paths`
and `zotero_batch_scanner._iter_live_vault_note_paths`: it walks
``*_review_note.md`` under `vault_root`, skips a small set of pipeline
artifact directories, and reads `combined_hash` + `zotero_parent_key`
from each note's YAML frontmatter.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

import yaml

if os.name == "nt":
    import msvcrt

    def _lock_file(fh):
        # Lock a fixed 1-byte region at offset 0 as a cross-process mutex.
        # Writes still land at EOF because the file is opened in "a" mode.
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _unlock_file(fh):
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_file(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _unlock_file(fh):
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


_FRONTMATTER_BLOCK_RE = re.compile(r"^---\r?\n(.*?)\r?\n---\r?\n?", re.DOTALL)

# Mirrors zotero_batch_scanner.LIVE_VAULT_EXCLUDED_*. Kept duplicated so this
# module has zero scanner-side imports.
_EXCLUDED_RELATIVE_PREFIXES = (
    "progress/gate_backups/",
    "progress/gate_reports/",
    "progress/version_snapshots/",
    "progress/schema_migration/",
    "progress/taxonomy_discovery/",
    "progress/pipeline_logs/",
    "progress/pipeline_reports/",
)
_EXCLUDED_PATH_PARTS = {".claude", ".obsidian", "__pycache__", ".stfolder"}


def _read_note_frontmatter(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    match = _FRONTMATTER_BLOCK_RE.match(text)
    if not match:
        return {}
    try:
        payload = yaml.safe_load(match.group(1)) or {}
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _iter_review_note_paths(vault_root: Path):
    vault_root = vault_root.resolve()
    for path in vault_root.rglob("*_review_note.md"):
        try:
            relative = path.resolve().relative_to(vault_root).as_posix()
        except ValueError:
            continue
        if any(relative.startswith(prefix) for prefix in _EXCLUDED_RELATIVE_PREFIXES):
            continue
        if any(part in _EXCLUDED_PATH_PARTS or part.startswith(".tmp") for part in Path(relative).parts):
            continue
        yield path.resolve()


def _scan_vault(vault_root: Path):
    """Walk the vault and return (hash_to_paths, parent_key_to_paths).

    Both maps are keyed by the values found in note frontmatter and point
    at lists of resolved Path objects. A note missing both keys is silently
    skipped — it provides no recovery information.
    """
    hash_to_paths: dict[str, list[Path]] = {}
    parent_key_to_paths: dict[str, list[Path]] = {}
    if not vault_root.exists():
        return hash_to_paths, parent_key_to_paths

    for note_path in _iter_review_note_paths(vault_root):
        fm = _read_note_frontmatter(note_path)
        if not fm:
            continue
        # Honor BOTH stable and legacy hashes when present in frontmatter.
        # legacy_combined_hash was added 2026-05; older notes still have only
        # combined_hash. New notes emit both when they differ.
        for key in ("combined_hash", "legacy_combined_hash"):
            value = str(fm.get(key) or "").strip()
            if value:
                hash_to_paths.setdefault(value, []).append(note_path)
        parent_key = str(fm.get("zotero_parent_key") or "").strip()
        if parent_key:
            parent_key_to_paths.setdefault(parent_key, []).append(note_path)

    return hash_to_paths, parent_key_to_paths


def _hashes_from_cached_index(payload) -> tuple[dict, dict]:
    """Convert a JSON-shape cached note_index into in-memory dicts.

    The cached form (written by `zotero_batch_scanner.write_live_note_index_file`)
    is a JSON object with `combined_hash` and `zotero_parent_key` sub-objects
    mapping to lists of stringified paths. We re-resolve the strings to Path
    objects and drop entries whose files have since disappeared.
    """
    hash_to_paths: dict[str, list[Path]] = {}
    parent_key_to_paths: dict[str, list[Path]] = {}
    if not isinstance(payload, dict):
        return hash_to_paths, parent_key_to_paths

    for src_key, dest in (
        ("combined_hash", hash_to_paths),
        ("zotero_parent_key", parent_key_to_paths),
    ):
        bucket = payload.get(src_key) or {}
        if not isinstance(bucket, dict):
            continue
        for key, raw_paths in bucket.items():
            paths = raw_paths if isinstance(raw_paths, list) else []
            for raw_path in paths:
                try:
                    resolved = Path(str(raw_path)).resolve()
                except OSError:
                    continue
                if not resolved.exists():
                    continue
                dest.setdefault(str(key), []).append(resolved)
    return hash_to_paths, parent_key_to_paths


def _load_ledger_hashes(history_path: Path) -> set[str]:
    if not history_path.exists():
        return set()
    return {
        line.strip()
        for line in history_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }


class DedupIndex:
    """In-memory union of ledger hashes and vault-derived hash/parent-key maps.

    Construction is eager: `build()` reads the ledger and scans the vault
    once. Subsequent `lookup()` calls are O(1). `append()` writes through
    to the ledger atomically and updates the in-memory hash set.

    The class is intentionally not thread-safe — the pipeline is
    single-process, single-writer.
    """

    def __init__(
        self,
        *,
        history_path: Path,
        ledger_hashes: set[str],
        hash_to_paths: dict[str, list[Path]],
        parent_key_to_paths: dict[str, list[Path]],
    ):
        self._history_path = Path(history_path)
        self._ledger_hashes = set(ledger_hashes)
        self._hash_to_paths = dict(hash_to_paths)
        self._parent_key_to_paths = dict(parent_key_to_paths)

    @classmethod
    def build(
        cls,
        *,
        history_path,
        vault_root,
        cached_note_index=None,
    ) -> "DedupIndex":
        """Read the ledger and scan the vault.

        If `cached_note_index` is provided (as the dict shape produced by
        `zotero_batch_scanner.build_live_note_index`), it is preferred over
        a fresh vault rglob — the batch scanner pre-builds it once per
        invocation and pipes it through `--note-index-file` to each
        per-paper subprocess to avoid redundant I/O.
        """
        history_path = Path(history_path)
        vault_root = Path(vault_root)
        ledger_hashes = _load_ledger_hashes(history_path)
        if cached_note_index is not None:
            hash_to_paths, parent_key_to_paths = _hashes_from_cached_index(cached_note_index)
        else:
            hash_to_paths, parent_key_to_paths = _scan_vault(vault_root)
        return cls(
            history_path=history_path,
            ledger_hashes=ledger_hashes,
            hash_to_paths=hash_to_paths,
            parent_key_to_paths=parent_key_to_paths,
        )

    def lookup(
        self,
        *,
        combined_hash: Optional[str] = None,
        legacy_combined_hash: Optional[str] = None,
        zotero_parent_key: Optional[str] = None,
    ):
        """Return (matched_hash, note_path_or_None) on hit, else None.

        Preference order:
          1. combined_hash in vault    → (combined_hash, vault_path)
          2. legacy_combined_hash in vault → (legacy, vault_path)
          3. parent_key in vault       → (combined_hash, vault_path)
          4. combined_hash in ledger   → (combined_hash, None)
          5. legacy_combined_hash in ledger → (legacy, None)

        Vault hits always carry a path (useful for the skip-log message);
        ledger-only hits return None for the path because the ledger
        records nothing else. Stage (3) returns the *current* combined_hash
        even though the match was via parent_key — callers append the
        canonical stable hash to the ledger, never the legacy variant.
        """
        for hash_value in (combined_hash, legacy_combined_hash):
            if hash_value and hash_value in self._hash_to_paths:
                paths = self._hash_to_paths[hash_value]
                if paths:
                    return hash_value, paths[0]
        if zotero_parent_key and zotero_parent_key in self._parent_key_to_paths:
            paths = self._parent_key_to_paths[zotero_parent_key]
            if paths and combined_hash:
                return combined_hash, paths[0]
        for hash_value in (combined_hash, legacy_combined_hash):
            if hash_value and hash_value in self._ledger_hashes:
                return hash_value, None
        return None

    def append(self, combined_hash: str) -> None:
        """Append `combined_hash` to the ledger if absent (cross-process safe).

        Implementation: single-line append with the file opened in "a" mode,
        holding an OS-level exclusive lock (msvcrt on Windows, flock
        elsewhere) for the duration of the write. Earlier versions did a
        read-modify-replace of the whole file, which loses entries when the
        batch scanner's concurrent workers publish at the same time.
        Duplicate lines (two processes racing the same hash past their
        in-memory checks) are harmless — every reader loads the ledger into
        a set.
        """
        if not combined_hash:
            return
        if combined_hash in self._ledger_hashes:
            return
        history_path = self._history_path
        history_path.parent.mkdir(parents=True, exist_ok=True)
        with open(history_path, "a+b") as fh:
            _lock_file(fh)
            try:
                # Repair a hand-edited file whose last line lost its newline —
                # otherwise the new hash would glue onto it.
                fh.seek(0, os.SEEK_END)
                needs_newline = False
                if fh.tell() > 0:
                    fh.seek(-1, os.SEEK_END)
                    needs_newline = fh.read(1) != b"\n"
                payload = ("\n" if needs_newline else "") + combined_hash + "\n"
                fh.write(payload.encode("utf-8"))
                fh.flush()
            finally:
                _unlock_file(fh)
        self._ledger_hashes.add(combined_hash)

    @property
    def ledger_hashes(self) -> set[str]:
        """Read-only view of the in-memory ledger hash set."""
        return set(self._ledger_hashes)

    def covers(self, combined_hash: str, legacy_combined_hash: Optional[str] = None) -> bool:
        """Cheap boolean variant of `lookup` for callers that only need to
        know whether a paper has been processed at all (no path needed)."""
        for hash_value in (combined_hash, legacy_combined_hash):
            if hash_value and (
                hash_value in self._ledger_hashes
                or hash_value in self._hash_to_paths
            ):
                return True
        return False


__all__ = ["DedupIndex"]
