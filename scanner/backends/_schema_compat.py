"""
Convert Vertex/Gemini-dialect JSON schemas to standard JSON Schema.

Domain packs ship their stage schemas in the Vertex AI dialect (uppercase
`type` values like "OBJECT"/"STRING", `propertyOrdering`, `nullable`).
Vertex and the Gemini API consume that dialect natively; Anthropic tool
`input_schema` and OpenAI function `parameters` expect standard JSON
Schema. This module bridges the gap at the backend boundary so packs
only ever author ONE schema file.
"""

from __future__ import annotations

_TYPE_MAP = {
    "STRING": "string",
    "NUMBER": "number",
    "INTEGER": "integer",
    "BOOLEAN": "boolean",
    "ARRAY": "array",
    "OBJECT": "object",
    "NULL": "null",
}


def to_json_schema(schema):
    """Recursively translate a Vertex-dialect schema to standard JSON Schema.

    - Uppercase ``type`` values are lowercased ("OBJECT" -> "object").
    - ``propertyOrdering`` (a Vertex-only hint) is dropped.
    - ``nullable: true`` becomes a ``["<type>", "null"]`` type union.

    Standard JSON Schema input passes through unchanged, so callers can
    apply this unconditionally.
    """
    if isinstance(schema, list):
        return [to_json_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    out = {}
    for key, value in schema.items():
        if key in ("propertyOrdering", "nullable"):
            continue
        if key == "type" and isinstance(value, str):
            out[key] = _TYPE_MAP.get(value, value)
        else:
            out[key] = to_json_schema(value)

    if schema.get("nullable") is True:
        declared = out.get("type")
        if isinstance(declared, str) and declared != "null":
            out["type"] = [declared, "null"]
    return out


__all__ = ["to_json_schema"]
