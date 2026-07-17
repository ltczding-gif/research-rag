"""Cross-module parity test: scanner/zotero_client.py:get_parent_key and
service/build_pdf_db.py:get_parent_key_by_pdf_path MUST produce identical
output for the same input. They are intentionally duplicated (service/ and
scanner/ deploy independently). Mirrors tests/test_hash_parity.py."""

from __future__ import annotations

import ast
import hashlib  # used by exec'd module
import os  # used by exec'd module
import re  # used by exec'd module
import sqlite3
from pathlib import Path

import pytest

from zotero_client import get_parent_key


def _load_service_get_parent_key():
    """AST-extract get_parent_key_by_pdf_path from service/build_pdf_db.py
    and exec it in an isolated namespace (importing the module would trigger
    extract_pdf_groups_from_notes side effects)."""
    repo_root = Path(__file__).resolve().parent.parent
    src = (repo_root / "service" / "build_pdf_db.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn_src = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "get_parent_key_by_pdf_path":
            fn_src = ast.get_source_segment(src, node)
            break
    if fn_src is None:
        pytest.skip("service/build_pdf_db.py:get_parent_key_by_pdf_path not found")
    namespace = {"os": os, "re": re, "zotero_sqlite": sqlite3, "ZOTERO_DB": None}
    exec(fn_src, namespace)
    return namespace["get_parent_key_by_pdf_path"]


@pytest.fixture
def service_get_parent_key():
    return _load_service_get_parent_key()


@pytest.fixture
def fixture_db(tmp_path: Path) -> Path:
    """Same tiny Zotero schema as test_zotero_client.py."""
    db_path = tmp_path / "zotero.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE items (
            itemID INTEGER PRIMARY KEY,
            key TEXT NOT NULL UNIQUE
        );
        CREATE TABLE itemAttachments (
            itemID INTEGER PRIMARY KEY,
            parentItemID INTEGER,
            path TEXT
        );
    """)
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (1, "PARENT01"))
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (2, "ATTACH01"))
    conn.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, path) VALUES (?, ?, ?)",
        (2, 1, "storage:paper.pdf"),
    )
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (3, "PARENT02"))
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (4, "ATTACH02"))
    conn.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, path) VALUES (?, ?, ?)",
        (4, 3, "attachments:other.pdf"),
    )
    conn.commit()
    conn.close()
    return db_path


def test_storage_path_results_match(fixture_db, service_get_parent_key, tmp_path):
    pdf = tmp_path / "storage" / "ATTACH01" / "paper.pdf"
    scanner_key = get_parent_key(pdf, zotero_db=fixture_db)
    service_key = service_get_parent_key(str(pdf), zotero_db=fixture_db)
    assert scanner_key == service_key == "PARENT01"


def test_like_fallback_results_match(fixture_db, service_get_parent_key, tmp_path):
    pdf = tmp_path / "elsewhere" / "other.pdf"
    scanner_key = get_parent_key(pdf, zotero_db=fixture_db)
    service_key = service_get_parent_key(str(pdf), zotero_db=fixture_db)
    assert scanner_key == service_key == "PARENT02"


def test_no_match_results_match(fixture_db, service_get_parent_key, tmp_path):
    pdf = tmp_path / "missing_paper.pdf"
    scanner_key = get_parent_key(pdf, zotero_db=fixture_db)
    service_key = service_get_parent_key(str(pdf), zotero_db=fixture_db)
    assert scanner_key is None
    assert service_key is None


def test_strategy_precedence_results_match(tmp_path):
    """When a path matches the storage layout AND a same-named attachment
    exists under a different parent, both implementations must prefer the
    storage-key match (Strategy 1) over the LIKE fallback (Strategy 2).

    Without this test, a regression that drops Strategy 1's early-return
    would silently pick whichever LIKE match the SQL engine returned first."""
    db_path = tmp_path / "z.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE);
        CREATE TABLE itemAttachments (
            itemID INTEGER PRIMARY KEY, parentItemID INTEGER, path TEXT
        );
    """)
    # Real attachment: parent PARENT01, attachment ATTACH01, filename paper.pdf
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (1, "PARENT01"))
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (2, "ATTACH01"))
    conn.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, path) VALUES (?, ?, ?)",
        (2, 1, "storage:paper.pdf"),
    )
    # Decoy attachment with the same filename under a different parent
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (3, "PARENT_DECOY"))
    conn.execute("INSERT INTO items (itemID, key) VALUES (?, ?)", (4, "ATTACH_X"))
    conn.execute(
        "INSERT INTO itemAttachments (itemID, parentItemID, path) VALUES (?, ?, ?)",
        (4, 3, "attachments:somewhere/paper.pdf"),
    )
    conn.commit()
    conn.close()

    pdf = tmp_path / "storage" / "ATTACH01" / "paper.pdf"
    service_fn = _load_service_get_parent_key()
    scanner_key = get_parent_key(pdf, zotero_db=db_path)
    service_key = service_fn(str(pdf), zotero_db=db_path)
    # Both must prefer the storage-key match (PARENT01), not the decoy
    assert scanner_key == service_key == "PARENT01"
