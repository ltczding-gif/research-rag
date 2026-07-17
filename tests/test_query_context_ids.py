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
