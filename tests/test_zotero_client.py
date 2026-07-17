"""Smoke tests for scanner/zotero_client.py against an in-memory SQLite fixture."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from zotero_client import get_parent_key


# Minimum schema we need for the two-strategy lookup. Real Zotero's schema is
# much larger but the JOINs in get_parent_key only touch these two tables and
# these columns.
_FIXTURE_SCHEMA = """
CREATE TABLE items (
    itemID INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE
);

CREATE TABLE itemAttachments (
    itemID INTEGER PRIMARY KEY,
    parentItemID INTEGER,
    path TEXT,
    FOREIGN KEY (itemID) REFERENCES items(itemID),
    FOREIGN KEY (parentItemID) REFERENCES items(itemID)
);
"""


@pytest.fixture
def zotero_db(tmp_path: Path) -> Path:
    """Tiny Zotero-shaped DB with one parent item + one attachment."""
    db_path = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_FIXTURE_SCHEMA)
    # Parent paper
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (1, "PARENT01"))
    # Attachment with a Zotero-style 8-char key
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (2, "ATTACH01"))
    conn.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, path) VALUES (?, ?, ?)",
        (2, 1, "storage:paper.pdf"),
    )
    # A second parent + attachment to ensure JOINs don't accidentally cross-pollinate
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (3, "PARENT02"))
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (4, "ATTACH02"))
    conn.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, path) VALUES (?, ?, ?)",
        (4, 3, "attachments:other.pdf"),
    )
    conn.commit()
    conn.close()
    return db_path


def test_storage_key_path_returns_parent(zotero_db, tmp_path):
    """Strategy 1: a path matching .../storage/<KEY>/<filename> should be
    looked up by the attachment key, returning the parent's key."""
    fake_pdf = tmp_path / "Zotero" / "storage" / "ATTACH01" / "paper.pdf"
    result = get_parent_key(fake_pdf, zotero_db=zotero_db)
    assert result == "PARENT01"


def test_storage_key_path_with_lowercase_key(zotero_db, tmp_path):
    """Lowercase keys must also match — Zotero generates base62 (mixed case)."""
    # Insert a lowercase-key attachment
    conn = sqlite3.connect(str(zotero_db))
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (5, "PARENT03"))
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (6, "lower3xy"))
    conn.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, path) VALUES (?, ?, ?)",
        (6, 5, "storage:other.pdf"),
    )
    conn.commit()
    conn.close()

    fake_pdf = tmp_path / "storage" / "lower3xy" / "other.pdf"
    result = get_parent_key(fake_pdf, zotero_db=zotero_db)
    assert result == "PARENT03"


def test_like_fallback_finds_match_by_filename(zotero_db, tmp_path):
    """Strategy 2: linked-file path that doesn't match the storage layout
    should fall through to a LIKE substring match on the filename."""
    # Path doesn't have storage/<KEY>/ structure
    linked_pdf = tmp_path / "elsewhere" / "other.pdf"
    result = get_parent_key(linked_pdf, zotero_db=zotero_db)
    assert result == "PARENT02"  # matched by 'other.pdf' substring


def test_no_match_returns_none(zotero_db, tmp_path):
    """A filename not present in any attachment row returns None."""
    nonexistent = tmp_path / "totally_unrelated_paper.pdf"
    assert get_parent_key(nonexistent, zotero_db=zotero_db) is None


def test_storage_key_takes_precedence_over_like(zotero_db, tmp_path):
    """When the storage-key strategy hits, the LIKE fallback is not consulted —
    even if there's a same-filename collision under a different parent."""
    # Insert a third item that would collide with paper.pdf under the LIKE strategy
    conn = sqlite3.connect(str(zotero_db))
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (7, "PARENT99"))
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (8, "DECOYKEY"))
    conn.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, path) VALUES (?, ?, ?)",
        (8, 7, "attachments:something/paper.pdf"),
    )
    conn.commit()
    conn.close()

    # Path uses the storage layout for ATTACH01, so we expect PARENT01
    # (not the decoy PARENT99 that LIKE would also match).
    fake_pdf = tmp_path / "storage" / "ATTACH01" / "paper.pdf"
    assert get_parent_key(fake_pdf, zotero_db=zotero_db) == "PARENT01"


def test_invalid_db_path_returns_none(tmp_path):
    """A nonexistent or unreadable DB shouldn't raise; just return None."""
    # sqlite3.connect creates the file on missing path; pass a directory instead
    # to force a connection error.
    bad = tmp_path  # a directory, not a file
    result = get_parent_key("anything.pdf", zotero_db=bad)
    assert result is None
