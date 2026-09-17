from __future__ import annotations

import ast
from pathlib import Path


SOURCE = Path(__file__).resolve().parent.parent / "service" / "query_server.py"


def _load_helper():
    source = SOURCE.read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_neighbor_chunk_ids"
    )
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(SOURCE), "exec"), namespace)
    return namespace["_neighbor_chunk_ids"]


def test_neighbor_ids_follow_content_hash_schema():
    helper = _load_helper()
    hit = "group_groupdigest_file_filedigest_chunk_7"
    assert helper(hit, 7) == (
        "group_groupdigest_file_filedigest_chunk_6",
        "group_groupdigest_file_filedigest_chunk_8",
    )


def test_neighbor_ids_remain_compatible_with_legacy_schema():
    helper = _load_helper()
    assert helper("group_2_file_0_chunk_0", 0) == (None, "group_2_file_0_chunk_1")


def test_production_compact_ids_keep_parent_hash_and_attachment_boundary():
    helper = _load_helper()
    assert helper("paper_PARENT_digest_f0_c31", 31) == (
        "paper_PARENT_digest_f0_c30", "paper_PARENT_digest_f0_c32")
    assert helper("paper_PARENT_digest_f1_c0", 0) == (None, "paper_PARENT_digest_f1_c1")


def test_neighbor_uses_actual_suffix_when_old_metadata_disagrees():
    assert _load_helper()("paper_PARENT_digest_f0_c31", 0) == (
        "paper_PARENT_digest_f0_c30", "paper_PARENT_digest_f0_c32")
