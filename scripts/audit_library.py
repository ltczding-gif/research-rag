"""Read-only inventory audit for a Chroma SQLite store.

This intentionally inspects only what is indexed in Chroma.  It does not open
PDFs, query Zotero, instantiate Chroma's client, or contact an embedding model.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
from typing import Any


REQUIRED_TABLES = {
    "collections": {"id", "name", "dimension", "schema_str"},
    "segments": {"id", "collection", "scope"},
    "embeddings": {"id", "segment_id", "embedding_id"},
    "embedding_metadata": {
        "id", "key", "string_value", "int_value", "float_value", "bool_value"
    },
}


class AuditError(RuntimeError):
    """The requested local Chroma inventory cannot be audited safely."""


def _database_file(chroma_path: str | Path) -> Path:
    path = Path(chroma_path).expanduser()
    return path / "chroma.sqlite3" if path.is_dir() else path


def _connect_read_only(chroma_path: str | Path) -> sqlite3.Connection:
    database = _database_file(chroma_path)
    if not database.is_file():
        raise AuditError(f"Chroma SQLite database was not found: {database}")
    try:
        # Do not use immutable=1: mode=ro must still see a live WAL snapshot.
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        return connection
    except sqlite3.Error as exc:
        raise AuditError(f"Cannot open Chroma SQLite database read-only: {exc}") from exc


def _validate_schema(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).fetchall()
    available = {row[0] for row in rows}
    missing_tables = sorted(set(REQUIRED_TABLES) - available)
    if missing_tables:
        raise AuditError("Unsupported or incomplete Chroma schema; missing table(s): " + ", ".join(missing_tables))
    for table, required_columns in REQUIRED_TABLES.items():
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        missing_columns = sorted(required_columns - columns)
        if missing_columns:
            raise AuditError(
                f"Unsupported Chroma schema; {table} is missing column(s): "
                + ", ".join(missing_columns)
            )


def _nonempty(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _stored_embedding_config(schema_str: Any) -> dict[str, str | None]:
    """Extract only safe provider/model labels from Chroma's opaque schema JSON."""
    result: dict[str, str | None] = {"provider": None, "model": None}
    if not _nonempty(schema_str):
        return result
    try:
        value = json.loads(schema_str)
    except (TypeError, ValueError):
        return result

    # Chroma 1.5 stores this as
    # {"embedding_function": {"type": "known", "name": "ollama",
    #   "config": {"model_name": "...", "url": "..."}}}.  Read only
    # the two labels needed for identity, never the rest of config.
    def walk(node: Any) -> None:
        if isinstance(node, dict):
            function = node.get("embedding_function")
            if isinstance(function, dict):
                name = function.get("name")
                config = function.get("config")
                model_name = config.get("model_name") if isinstance(config, dict) else None
                if result["provider"] is None and _nonempty(name) and _nonempty(model_name):
                    result["provider"] = name.strip()
                if result["model"] is None and _nonempty(name) and _nonempty(model_name):
                    result["model"] = model_name.strip()
            for child in node.values():
                if result["provider"] is not None and result["model"] is not None:
                    return
                walk(child)
        elif isinstance(node, list):
            for child in node:
                if result["provider"] is not None and result["model"] is not None:
                    return
                walk(child)

    walk(value)
    return result


SUMMARY_SQL = """
SELECT s.collection AS collection_id, COUNT(e.id) AS embedding_count
FROM segments AS s
LEFT JOIN embeddings AS e ON e.segment_id = s.id
GROUP BY s.collection
"""


METADATA_COVERAGE_SQL = """
SELECT s.collection AS collection_id, m.key,
       SUM(CASE
           WHEN m.key = 'schema_version'
                AND (m.string_value IS NOT NULL OR m.int_value IS NOT NULL
                     OR m.float_value IS NOT NULL OR m.bool_value IS NOT NULL) THEN 1
           WHEN m.key <> 'schema_version' AND NULLIF(TRIM(m.string_value), '') IS NOT NULL THEN 1
           ELSE 0
       END) AS present_count
FROM segments AS s
JOIN embeddings AS e ON e.segment_id = s.id
JOIN embedding_metadata AS m ON m.id = e.id
WHERE m.key IN ('zotero_parent_key', 'pdf_path', 'source_path',
                'id_schema', 'generation_id', 'schema_version')
GROUP BY s.collection, m.key
"""


PARENT_VALUES_SQL = """
SELECT s.collection AS collection_id, m.string_value AS parent_key
FROM segments AS s
JOIN embeddings AS e ON e.segment_id = s.id
JOIN embedding_metadata AS m ON m.id = e.id
WHERE m.key = 'zotero_parent_key'
  AND m.string_value IS NOT NULL
  AND TRIM(m.string_value) <> ''
"""


COLLECTIONS_SQL = "SELECT id, name, dimension, schema_str FROM collections ORDER BY name"


PATHS_SQL = """
SELECT DISTINCT s.collection AS collection_id, m.key, m.string_value AS path
    FROM segments AS s
JOIN embeddings AS e ON e.segment_id = s.id
JOIN embedding_metadata AS m ON m.id = e.id
WHERE m.key IN ('pdf_path', 'source_path')
  AND m.string_value IS NOT NULL
  AND TRIM(m.string_value) <> ''
"""


def audit_library(chroma_path: str | Path, *, include_paths: bool = False) -> dict[str, Any]:
    """Return a privacy-preserving audit of the current indexed inventory."""
    connection = _connect_read_only(chroma_path)
    try:
        _validate_schema(connection)
        counts = {
            str(row["collection_id"]): int(row["embedding_count"])
            for row in connection.execute(SUMMARY_SQL)
        }
        coverage: dict[str, dict[str, int]] = {}
        for row in connection.execute(METADATA_COVERAGE_SQL):
            coverage.setdefault(str(row["collection_id"]), {})[row["key"]] = int(row["present_count"] or 0)
        parent_values: dict[str, set[str]] = {}
        all_parent_values: set[str] = set()
        for row in connection.execute(PARENT_VALUES_SQL):
            collection_id = str(row["collection_id"])
            parent_key = row["parent_key"].strip()
            parent_values.setdefault(collection_id, set()).add(parent_key)
            all_parent_values.add(parent_key)
        collection_rows = connection.execute(COLLECTIONS_SQL).fetchall()
        collections: dict[str, dict[str, Any]] = {}
        for row in collection_rows:
            collection_id = str(row["id"])
            count = counts.get(collection_id, 0)
            present = coverage.get(collection_id, {})
            schema_coverage = {
                key: {"present": present.get(key, 0), "missing": count - present.get(key, 0)}
                for key in ("id_schema", "generation_id", "schema_version")
            }
            id_schema_coverage = schema_coverage["id_schema"]
            generation_coverage = schema_coverage["generation_id"]
            version_coverage = schema_coverage["schema_version"]
            if not count:
                classification = "empty"
            elif id_schema_coverage["present"] == count or (
                generation_coverage["present"] == count and version_coverage["present"] == count
            ):
                classification = "canonical"
            elif any(item["present"] for item in schema_coverage.values()):
                classification = "mixed"
            else:
                classification = "legacy"
            collections[collection_id] = {
                "name": row["name"],
                "embedding_count": count,
                "embedding_dimension": row["dimension"],
                "distinct_zotero_parent_keys": len(parent_values.get(collection_id, set())),
                "metadata_missing": {
                    key: count - present.get(key, 0)
                    for key in ("zotero_parent_key", "pdf_path", "source_path")
                },
                "schema_generation": {
                    "classification": classification,
                    "metadata_coverage": schema_coverage,
                },
                "stored_embedding_config": _stored_embedding_config(row["schema_str"]),
                "source_paths": {
                    "indexed_unique": 0,
                    "existing": 0,
                    "missing": 0,
                },
            }

        missing_paths: dict[str, list[str]] = {}
        seen_paths: set[tuple[str, str]] = set()
        for row in connection.execute(PATHS_SQL):
            collection_id = str(row["collection_id"])
            path = row["path"]
            key = (collection_id, path)
            # A pdf_path and source_path that point at the same source are checked once.
            if key in seen_paths or collection_id not in collections:
                continue
            seen_paths.add(key)
            stats = collections[collection_id]["source_paths"]
            stats["indexed_unique"] += 1
            if os.path.exists(path):
                stats["existing"] += 1
            else:
                stats["missing"] += 1
                if include_paths:
                    missing_paths.setdefault(collection_id, []).append(path)

        ordered = []
        for collection_id, item in collections.items():
            if include_paths and missing_paths.get(collection_id):
                item["source_paths"]["missing_paths"] = missing_paths[collection_id]
            ordered.append(item)
        report: dict[str, Any] = {
            "scope": {
                "indexed_inventory_only": True,
                "description": "This audit covers the current indexed Chroma inventory, not all items in Zotero.",
            },
            "collections": ordered,
            "totals": {
                "collections": len(ordered),
                "embeddings": sum(item["embedding_count"] for item in ordered),
                "distinct_zotero_parent_keys": len(all_parent_values),
            },
            "limitations": [
                "No relevance labels are stored here, so this audit cannot infer recall.",
                "Canonical and legacy labels are metadata hints only; this audit does not verify source provenance.",
                "Only unique indexed pdf_path/source_path values are checked for existence; PDFs are not opened or hashed.",
            ],
        }
        return report
    except sqlite3.Error as exc:
        raise AuditError(f"Chroma SQLite query failed: {exc}") from exc
    finally:
        connection.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only Chroma indexed-inventory audit.")
    parser.add_argument("--chroma-path", required=True, help="Chroma directory or chroma.sqlite3 path")
    parser.add_argument("--output", required=True, help="JSON report destination")
    parser.add_argument("--include-paths", action="store_true", help="Include missing source paths in this private report")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> dict[str, Any]:
    """Run the audit, write JSON, and return the report for programmatic callers."""
    args = _parse_args(argv)
    report = audit_library(args.chroma_path, include_paths=args.include_paths)
    output = Path(args.output).expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def _cli() -> int:
    try:
        report = main()
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except AuditError as exc:
        print(f"audit failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
