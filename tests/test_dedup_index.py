"""Tests for `scanner/dedup_index.py`.

Covers the four-layer recovery promise:
  1. Vault hit by combined_hash      → returns (hash, path)
  2. Vault hit by legacy_combined_hash → returns (legacy, path)
  3. Vault hit by zotero_parent_key  → returns (combined_hash, path)
  4. Ledger-only hit                 → returns (hash, None)

Plus: atomic ledger append survives a simulated mid-write crash.
"""

from __future__ import annotations

from dedup_index import DedupIndex


# --- Helpers ---------------------------------------------------------------


_FRONTMATTER_TEMPLATE = """---
title_en: dummy
combined_hash: {combined_hash}
{legacy_line}{parent_line}---

body
"""


def _write_note(
    vault: Path,
    *,
    name: str,
    combined_hash: str,
    legacy_combined_hash: str | None = None,
    zotero_parent_key: str | None = None,
) -> Path:
    legacy_line = (
        f"legacy_combined_hash: {legacy_combined_hash}\n" if legacy_combined_hash else ""
    )
    parent_line = (
        f"zotero_parent_key: {zotero_parent_key}\n" if zotero_parent_key else ""
    )
    content = _FRONTMATTER_TEMPLATE.format(
        combined_hash=combined_hash,
        legacy_line=legacy_line,
        parent_line=parent_line,
    )
    note_path = vault / name
    note_path.write_text(content, encoding="utf-8")
    return note_path


# --- 1. Vault recovery when ledger is deleted ------------------------------


def test_lookup_recovers_via_vault_hash_when_ledger_missing(tmp_path):
    """Even with no ledger file, a note with combined_hash in frontmatter
    must produce a dedup hit."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = _write_note(vault, name="paper_review_note.md", combined_hash="a" * 64)

    history_path = tmp_path / "processed_history.txt"
    assert not history_path.exists()

    index = DedupIndex.build(history_path=history_path, vault_root=vault)
    hit = index.lookup(combined_hash="a" * 64)
    assert hit is not None
    matched_hash, matched_path = hit
    assert matched_hash == "a" * 64
    assert matched_path == note.resolve()


def test_lookup_recovers_via_legacy_hash_when_ledger_missing(tmp_path):
    """Pre-2026-05 multi-PDF notes that were processed under the legacy
    algorithm must still be recognized after the ledger is wiped — the
    note carries `legacy_combined_hash` in frontmatter."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = _write_note(
        vault,
        name="legacy_review_note.md",
        combined_hash="b" * 64,
        legacy_combined_hash="c" * 64,
    )

    index = DedupIndex.build(
        history_path=tmp_path / "missing.txt",
        vault_root=vault,
    )
    # Caller arrives with the new stable hash + the recomputed legacy hash;
    # the legacy match (via the note's legacy_combined_hash field) should fire.
    hit = index.lookup(combined_hash="d" * 64, legacy_combined_hash="c" * 64)
    assert hit is not None
    matched_hash, matched_path = hit
    assert matched_hash == "c" * 64
    assert matched_path == note.resolve()


def test_lookup_recovers_via_parent_key_when_hashes_mismatch(tmp_path):
    """If the PDF bytes changed but the Zotero parent_key still matches
    an existing note, the dedup index treats it as a hit and returns the
    *current* combined_hash (not the note's stale hash)."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = _write_note(
        vault,
        name="parent_review_note.md",
        combined_hash="e" * 64,
        zotero_parent_key="ABCD1234",
    )

    index = DedupIndex.build(history_path=tmp_path / "x.txt", vault_root=vault)
    hit = index.lookup(combined_hash="f" * 64, zotero_parent_key="ABCD1234")
    assert hit is not None
    matched_hash, matched_path = hit
    # Returns the CURRENT hash (so the caller appends it to the ledger),
    # not the stale one stored in the note.
    assert matched_hash == "f" * 64
    assert matched_path == note.resolve()


def test_lookup_returns_none_when_nothing_matches(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    _write_note(vault, name="other_review_note.md", combined_hash="1" * 64)

    index = DedupIndex.build(history_path=tmp_path / "x.txt", vault_root=vault)
    assert index.lookup(combined_hash="9" * 64) is None
    assert index.lookup(combined_hash="9" * 64, zotero_parent_key="ZZZ") is None


# --- 2. Ledger-only fallback (no vault scan or empty vault) ----------------


def test_lookup_via_ledger_only_returns_no_path(tmp_path):
    history_path = tmp_path / "processed_history.txt"
    history_path.write_text("a" * 64 + "\n", encoding="utf-8")

    index = DedupIndex.build(
        history_path=history_path,
        vault_root=tmp_path / "nonexistent_vault",
    )
    hit = index.lookup(combined_hash="a" * 64)
    assert hit is not None
    matched_hash, matched_path = hit
    assert matched_hash == "a" * 64
    assert matched_path is None


# --- 3. Vault hit takes precedence over ledger-only entry ------------------


def test_vault_hit_takes_precedence_over_ledger(tmp_path):
    """When both layers know the hash, the vault entry wins so the caller
    can show the user *which* note already covers this PDF."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = _write_note(vault, name="dual_review_note.md", combined_hash="7" * 64)

    history_path = tmp_path / "processed_history.txt"
    history_path.write_text("7" * 64 + "\n", encoding="utf-8")

    index = DedupIndex.build(history_path=history_path, vault_root=vault)
    hit = index.lookup(combined_hash="7" * 64)
    assert hit is not None
    _matched_hash, matched_path = hit
    assert matched_path == note.resolve()


# --- 4. Atomic append ------------------------------------------------------


def test_append_creates_ledger_when_missing(tmp_path):
    history_path = tmp_path / "subdir" / "processed_history.txt"
    index = DedupIndex.build(
        history_path=history_path,
        vault_root=tmp_path / "no_vault",
    )
    index.append("a" * 64)
    assert history_path.exists()
    assert history_path.read_text(encoding="utf-8") == "a" * 64 + "\n"


def test_append_idempotent_for_known_hash(tmp_path):
    history_path = tmp_path / "processed_history.txt"
    history_path.write_text("a" * 64 + "\n", encoding="utf-8")

    index = DedupIndex.build(history_path=history_path, vault_root=tmp_path / "no_vault")
    index.append("a" * 64)  # already present
    index.append("a" * 64)  # again
    # File should still be exactly one line.
    assert history_path.read_text(encoding="utf-8") == "a" * 64 + "\n"


def test_append_preserves_existing_entries(tmp_path):
    history_path = tmp_path / "processed_history.txt"
    history_path.write_text("a" * 64 + "\n" + "b" * 64 + "\n", encoding="utf-8")

    index = DedupIndex.build(history_path=history_path, vault_root=tmp_path / "no_vault")
    index.append("c" * 64)

    lines = history_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines == ["a" * 64, "b" * 64, "c" * 64]


def test_append_from_independent_instances_loses_no_entries(tmp_path):
    """The lost-update scenario the old read-modify-replace implementation
    failed: two DedupIndex instances (standing in for two concurrent batch
    workers, each with its own in-memory view) append different hashes.
    With append-only writes, BOTH entries must survive in the file."""
    history_path = tmp_path / "processed_history.txt"
    history_path.write_text("a" * 64 + "\n", encoding="utf-8")

    worker1 = DedupIndex.build(history_path=history_path, vault_root=tmp_path / "no_vault")
    worker2 = DedupIndex.build(history_path=history_path, vault_root=tmp_path / "no_vault")

    worker1.append("b" * 64)
    worker2.append("c" * 64)  # old impl: rewrote from its stale snapshot, dropping b

    lines = history_path.read_text(encoding="utf-8").strip().splitlines()
    assert set(lines) == {"a" * 64, "b" * 64, "c" * 64}


def test_append_repairs_missing_trailing_newline(tmp_path):
    """A hand-edited ledger whose last line lost its newline must not have
    the next hash glued onto it."""
    history_path = tmp_path / "processed_history.txt"
    history_path.write_text("a" * 64, encoding="utf-8")  # no trailing \n

    index = DedupIndex.build(history_path=history_path, vault_root=tmp_path / "no_vault")
    index.append("b" * 64)

    lines = history_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines == ["a" * 64, "b" * 64]


# --- 5. covers() convenience -----------------------------------------------


def test_covers_returns_true_for_ledger_hit(tmp_path):
    history_path = tmp_path / "processed_history.txt"
    history_path.write_text("a" * 64 + "\n", encoding="utf-8")
    index = DedupIndex.build(history_path=history_path, vault_root=tmp_path / "no_vault")
    assert index.covers("a" * 64) is True
    assert index.covers("z" * 64) is False


def test_covers_accepts_legacy_variant(tmp_path):
    history_path = tmp_path / "processed_history.txt"
    history_path.write_text("legacy" + "0" * 58 + "\n", encoding="utf-8")
    index = DedupIndex.build(history_path=history_path, vault_root=tmp_path / "no_vault")
    assert index.covers("zzz", legacy_combined_hash="legacy" + "0" * 58) is True


# --- 6. End-to-end-ish: F1 (solo invocation auto-heal) ---------------------


def test_solo_invocation_skips_via_vault_when_ledger_empty(tmp_path):
    """Mirrors the `gemini_analyze_pdf.py:main()` happy path in solo mode:

      1. Ledger file does NOT exist (user wiped it).
      2. Vault still has the note with combined_hash in frontmatter.
      3. `DedupIndex.build()` reads both layers; lookup hits via vault.
      4. Caller appends the canonical hash to the ledger.
      5. Subsequent invocations short-circuit through the ledger.

    Without F1, step (3) would miss in solo mode and the paper would
    re-process from scratch."""
    vault = tmp_path / "vault"
    vault.mkdir()
    note = _write_note(vault, name="solo_review_note.md", combined_hash="a" * 64)
    history_path = tmp_path / "processed_history.txt"
    assert not history_path.exists()  # ledger genuinely missing

    # First invocation: empty ledger, vault recovers.
    index = DedupIndex.build(history_path=history_path, vault_root=vault)
    assert index.ledger_hashes == set()

    hit = index.lookup(combined_hash="a" * 64)
    assert hit is not None
    matched_hash, matched_path = hit
    assert matched_path == note.resolve()

    # Caller canonicalizes by appending to the ledger.
    index.append(matched_hash)
    assert "a" * 64 in index.ledger_hashes
    assert history_path.read_text(encoding="utf-8") == "a" * 64 + "\n"

    # Second invocation (fresh build, vault no longer reachable) —
    # ledger fast-path now keeps things skipped.
    index2 = DedupIndex.build(history_path=history_path, vault_root=tmp_path / "moved_vault")
    hit2 = index2.lookup(combined_hash="a" * 64)
    assert hit2 is not None
    matched_hash2, matched_path2 = hit2
    assert matched_hash2 == "a" * 64
    assert matched_path2 is None  # ledger-only hit (no vault path available)
