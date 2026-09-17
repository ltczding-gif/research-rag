from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import sys

import pytest


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from audit_library import AuditError, audit_library, main  # noqa: E402


def _fixture_database(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "chroma"
    root.mkdir()
    existing = tmp_path / "present.pdf"
    existing.write_bytes(b"not read by audit")
    database = root / "chroma.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE collections (id TEXT PRIMARY KEY, name TEXT, dimension INTEGER, schema_str TEXT);
            CREATE TABLE segments (id TEXT PRIMARY KEY, collection TEXT, scope TEXT);
            CREATE TABLE embeddings (id INTEGER PRIMARY KEY, segment_id TEXT, embedding_id TEXT);
            CREATE TABLE embedding_metadata (
                id INTEGER, key TEXT, string_value TEXT, int_value INTEGER,
                float_value REAL, bool_value INTEGER
            );
            """
        )
        connection.executemany(
            "INSERT INTO collections VALUES (?, ?, ?, ?)",
            [
                ("canonical", "papers-current", 384, json.dumps({"provider": "unrelated", "model": "unrelated", "defaults": {"float_list": {"vector_index": {"config": {"embedding_function": {"type": "known", "name": "ollama", "config": {"model_name": "qwen3-embedding:4b", "url": "never-report"}}}}}}})),
                ("legacy", "papers-legacy", 384, "{}"),
            ],
        )
        connection.executemany(
            "INSERT INTO segments VALUES (?, ?, ?)",
            [("s1", "canonical", "VECTOR"), ("s2", "legacy", "VECTOR")],
        )
        connection.executemany(
            "INSERT INTO embeddings VALUES (?, ?, ?)",
            [(1, "s1", "one"), (2, "s1", "two"), (3, "s2", "three")],
        )
        connection.executemany(
            "INSERT INTO embedding_metadata VALUES (?, ?, ?, ?, ?, ?)",
            [
                (1, "zotero_parent_key", "PARENT-A", None, None, None),
                (1, "pdf_path", str(existing), None, None, None),
                (1, "source_path", str(existing), None, None, None),
                (1, "id_schema", "canonical-pdf-v1", None, None, None),
                (1, "generation_id", "a" * 32, None, None, None),
                (1, "schema_version", None, 1, None, None),
                (2, "zotero_parent_key", "PARENT-A", None, None, None),
                (2, "pdf_path", str(tmp_path / "gone.pdf"), None, None, None),
                (2, "source_path", str(tmp_path / "gone.pdf"), None, None, None),
                (3, "pdf_path", str(tmp_path / "legacy.pdf"), None, None, None),
            ],
        )
    return root, database


def test_audit_counts_metadata_and_schema_generation(tmp_path):
    root, _ = _fixture_database(tmp_path)

    report = audit_library(root)
    current, legacy = report["collections"]
    assert report["totals"] == {"collections": 2, "embeddings": 3, "distinct_zotero_parent_keys": 1}
    assert current["embedding_count"] == 2
    assert current["distinct_zotero_parent_keys"] == 1
    assert current["metadata_missing"] == {"zotero_parent_key": 0, "pdf_path": 0, "source_path": 0}
    assert current["schema_generation"]["classification"] == "mixed"
    assert current["schema_generation"]["metadata_coverage"]["id_schema"] == {"present": 1, "missing": 1}
    assert current["stored_embedding_config"] == {"provider": "ollama", "model": "qwen3-embedding:4b"}
    assert "never-report" not in json.dumps(report)
    assert legacy["schema_generation"]["classification"] == "legacy"
    assert legacy["metadata_missing"]["zotero_parent_key"] == 1


def test_audit_checks_unique_indexed_paths_and_hides_them_by_default(tmp_path):
    root, _ = _fixture_database(tmp_path)

    report = audit_library(root)
    current = report["collections"][0]
    assert current["source_paths"] == {"indexed_unique": 2, "existing": 1, "missing": 1}
    assert str(tmp_path / "gone.pdf") not in json.dumps(report)

    detailed = audit_library(root, include_paths=True)
    assert detailed["collections"][0]["source_paths"]["missing_paths"] == [str(tmp_path / "gone.pdf")]


def test_main_writes_json_and_read_only_audit_does_not_change_database(tmp_path):
    root, database = _fixture_database(tmp_path)
    before = hashlib.sha256(database.read_bytes()).digest()
    output = tmp_path / "audit.json"

    report = main(["--chroma-path", str(root), "--output", str(output)])

    assert json.loads(output.read_text(encoding="utf-8")) == report
    assert hashlib.sha256(database.read_bytes()).digest() == before


def test_missing_database_and_bad_schema_fail_clearly(tmp_path):
    with pytest.raises(AuditError, match="not found"):
        audit_library(tmp_path / "absent")

    root = tmp_path / "bad"
    root.mkdir()
    sqlite3.connect(root / "chroma.sqlite3").close()
    with pytest.raises(AuditError, match="missing table"):
        audit_library(root)
