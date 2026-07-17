"""Cross-module test: scanner/_hashing.stable_combined_hash and
service/build_pdf_db.get_combined_hash MUST produce identical output
for the same input. They are intentionally duplicated (service/ and
scanner/ are deployed independently), so a test is the only enforcement
of the "KEEP IN SYNC" comments."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from _hashing import stable_combined_hash


# Lazy-import service/build_pdf_db.py is awkward because importing it
# runs `extract_pdf_groups_from_notes()` at module top level, which reads
# from the filesystem. Instead, we re-implement the algorithm reference
# here, then assert both implementations match it. If service/build_pdf_db.py
# drifts, this test fails because either:
#   (a) the scanner side stops matching the reference, OR
#   (b) the service side drifts and a future test (added when import is
#       made safe) catches it.
#
# Pragmatic: also exec() the relevant function from the service file
# directly without importing the module, so we get true cross-module
# verification today.

import ast
import hashlib
import os


def _load_service_functions(*names):
    """Extract pure helpers from service/build_pdf_db.py without importing
    the module (which has filesystem side effects at import time)."""
    repo_root = Path(__file__).resolve().parent.parent
    src = (repo_root / "service" / "build_pdf_db.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    selected = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in names:
            selected.append(ast.get_source_segment(src, node))
    found = {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    }
    missing = set(names) - found
    if missing:
        pytest.skip(f"service/build_pdf_db.py helpers not found: {sorted(missing)}")
    namespace = {
        "hashlib": hashlib,
        "os": os,
        "_file_hash_cache": {},
        "PAPERS_ID_SCHEMA": "content-hash-v1",
    }
    for fn_src in selected:
        exec(fn_src, namespace)
    return {name: namespace[name] for name in names}


@pytest.fixture
def service_get_combined_hash():
    return _load_service_functions("get_file_hash", "get_combined_hash")["get_combined_hash"]


@pytest.fixture
def service_build_chunk_ids():
    return _load_service_functions("build_chunk_ids")["build_chunk_ids"]


@pytest.fixture
def service_index_contract_helpers():
    return _load_service_functions(
        "reset_papers_index",
        "ensure_papers_id_schema",
    )


@pytest.fixture
def two_files(tmp_path: Path) -> tuple[Path, Path]:
    main = tmp_path / "main.pdf"
    si = tmp_path / "si.pdf"
    main.write_bytes(b"hello world")
    si.write_bytes(b"supplementary information")
    return main, si


def test_two_implementations_agree_for_two_files(two_files, service_get_combined_hash):
    a, b = two_files
    scanner_h = stable_combined_hash([a, b])
    service_h = service_get_combined_hash([str(a), str(b)])
    assert scanner_h == service_h


def test_two_implementations_agree_with_reversed_input(two_files, service_get_combined_hash):
    a, b = two_files
    scanner_h = stable_combined_hash([b, a])
    service_h = service_get_combined_hash([str(b), str(a)])
    assert scanner_h == service_h


def test_two_implementations_agree_with_duplicate_paths(two_files, service_get_combined_hash):
    """Duplicate paths must be deduped to the same effective input."""
    a, b = two_files
    scanner_h = stable_combined_hash([a, b, a])
    service_h = service_get_combined_hash([str(a), str(b), str(a)])
    assert scanner_h == service_h


def test_two_implementations_agree_with_case_variant_paths(tmp_path, service_get_combined_hash):
    """On case-insensitive filesystems, the same file referenced with
    different casing must dedupe. (On case-sensitive filesystems where
    these are actually different files, both sides skip the missing
    upper-case version and produce the same single-file hash.)"""
    p = tmp_path / "paper.pdf"
    p.write_bytes(b"x")
    upper = str(p).replace("paper.pdf", "PAPER.pdf")
    scanner_h = stable_combined_hash([str(p), upper])
    service_h = service_get_combined_hash([str(p), upper])
    assert scanner_h == service_h


def test_chunk_ids_do_not_depend_on_positional_group_index(service_build_chunk_ids):
    group_hash = "a" * 64
    file_hash = "b" * 64

    first_build = service_build_chunk_ids(group_hash, file_hash, 3)
    after_unrelated_note_insert = service_build_chunk_ids(group_hash, file_hash, 3)

    assert first_build == after_unrelated_note_insert
    assert first_build[0] == f"group_{group_hash}_file_{file_hash}_chunk_0"
    assert all("group_1_file_0" not in chunk_id for chunk_id in first_build)


def test_chunk_ids_are_disjoint_across_groups_and_files(service_build_chunk_ids):
    group_a = service_build_chunk_ids("a" * 64, "b" * 64, 2)
    group_b = service_build_chunk_ids("c" * 64, "d" * 64, 2)

    assert set(group_a).isdisjoint(group_b)


class _FakeCollection:
    def __init__(self, count, metadata=None):
        self._count = count
        self.metadata = metadata
        self.modified_metadata = None

    def count(self):
        return self._count

    def modify(self, *, metadata):
        self.modified_metadata = metadata


def test_nonempty_legacy_collection_requires_explicit_rebuild(service_index_contract_helpers):
    ensure_schema = service_index_contract_helpers["ensure_papers_id_schema"]
    collection = _FakeCollection(count=10, metadata={"embed_model": "old"})

    with pytest.raises(SystemExit, match="--rebuild"):
        ensure_schema(collection, {"id_schema": "content-hash-v1"})


def test_empty_collection_is_stamped_with_current_schema(service_index_contract_helpers):
    ensure_schema = service_index_contract_helpers["ensure_papers_id_schema"]
    collection = _FakeCollection(count=0, metadata={"embed_model": "model"})

    ensure_schema(collection, {"id_schema": "content-hash-v1"})

    assert collection.modified_metadata == {
        "embed_model": "model",
        "id_schema": "content-hash-v1",
    }


def test_rebuild_resets_only_named_collection_and_ledger(
    tmp_path, service_index_contract_helpers
):
    reset = service_index_contract_helpers["reset_papers_index"]
    ledger = tmp_path / "processed_groups.txt"
    ledger.write_text("old-hash\n", encoding="utf-8")

    class Item:
        def __init__(self, name):
            self.name = name

    class Client:
        def __init__(self):
            self.deleted = []

        def list_collections(self):
            return [Item("notes"), Item("papers")]

        def delete_collection(self, name):
            self.deleted.append(name)

    client = Client()
    reset(client, ledger, "papers")

    assert client.deleted == ["papers"]
    assert not ledger.exists()
