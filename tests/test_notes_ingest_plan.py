from __future__ import annotations

import ast
import hashlib
from pathlib import Path


SOURCE = Path(__file__).resolve().parent.parent / "service" / "build_notes_db.py"


def _load_helpers():
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    wanted = {"_file_sha256", "note_document_id", "plan_note_ingest"}
    nodes = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    namespace = {"hashlib": hashlib, "Path": Path}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace


def test_legacy_ledger_entry_is_reingested_when_collection_record_is_missing(tmp_path):
    helpers = _load_helpers()
    note = tmp_path / "paper_review_note.md"
    note.write_text("content", encoding="utf-8")

    to_process, upgrades = helpers["plan_note_ingest"](
        [note.name], {note.name: ""}, set(), tmp_path
    )

    assert to_process == [note.name]
    assert upgrades == []


def test_legacy_ledger_entry_upgrades_without_reembedding_when_record_exists(tmp_path):
    helpers = _load_helpers()
    note = tmp_path / "paper_review_note.md"
    note.write_text("content", encoding="utf-8")
    doc_id = helpers["note_document_id"](note.name)

    to_process, upgrades = helpers["plan_note_ingest"](
        [note.name], {note.name: ""}, {doc_id}, tmp_path
    )

    assert to_process == []
    assert upgrades[0][0] == note.name
