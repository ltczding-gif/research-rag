"""
Tests for scanner/backends/_schema_compat.py — the Vertex-dialect →
standard JSON Schema translation applied at the Anthropic/OpenAI backend
boundary.
"""

from __future__ import annotations

from backends._schema_compat import to_json_schema


def test_uppercase_types_are_lowercased_recursively():
    vertex = {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING", "enum": ["a", "b"]},
            "score": {"type": "NUMBER"},
            "tags": {"type": "ARRAY", "items": {"type": "STRING"}},
        },
        "required": ["name"],
    }
    out = to_json_schema(vertex)
    assert out["type"] == "object"
    assert out["properties"]["name"]["type"] == "string"
    assert out["properties"]["score"]["type"] == "number"
    assert out["properties"]["tags"]["type"] == "array"
    assert out["properties"]["tags"]["items"]["type"] == "string"
    # Non-type content passes through untouched.
    assert out["properties"]["name"]["enum"] == ["a", "b"]
    assert out["required"] == ["name"]


def test_property_ordering_is_dropped():
    vertex = {
        "type": "OBJECT",
        "propertyOrdering": ["a", "b"],
        "properties": {"a": {"type": "STRING"}, "b": {"type": "BOOLEAN"}},
    }
    out = to_json_schema(vertex)
    assert "propertyOrdering" not in out
    assert out["properties"]["b"]["type"] == "boolean"


def test_nullable_becomes_type_union():
    vertex = {"type": "STRING", "nullable": True}
    out = to_json_schema(vertex)
    assert out["type"] == ["string", "null"]
    assert "nullable" not in out


def test_standard_json_schema_passes_through_unchanged():
    standard = {
        "type": "object",
        "properties": {"x": {"type": ["integer", "null"]}},
        "additionalProperties": False,
    }
    assert to_json_schema(standard) == standard


def test_input_is_not_mutated():
    vertex = {"type": "OBJECT", "propertyOrdering": ["a"], "properties": {"a": {"type": "STRING"}}}
    to_json_schema(vertex)
    assert vertex["type"] == "OBJECT"
    assert "propertyOrdering" in vertex
