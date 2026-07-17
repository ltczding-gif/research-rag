"""Smoke tests for `scanner/bootstrap_domain_pack.py`.

Cover validate_pack invariant checks. Bootstrap-with-interactive-prompts
isn't tested end-to-end (would require monkeypatching input()), but the
helper functions that mutate files are covered separately.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest


# Add scanner/ to sys.path is handled by tests/conftest.py.
# Module name has hyphens-to-underscores via the script's filename.
import bootstrap_domain_pack as bdp


@pytest.fixture
def fresh_pack(tmp_path):
    """Build a minimal valid pack on disk for the validator to check."""
    pack = tmp_path / "fakefield"
    (pack / "prompts").mkdir(parents=True)
    (pack / "schemas").mkdir(parents=True)
    (pack / "templates").mkdir(parents=True)
    (pack / "config").mkdir(parents=True)

    (pack / "pack.yaml").write_text("name: fakefield\n", encoding="utf-8")
    (pack / "prompts" / "document_profiler.system.txt").write_text("x", encoding="utf-8")
    (pack / "prompts" / "note_generator.system.txt").write_text("x", encoding="utf-8")
    (pack / "prompts" / "seed_terms_guidance.txt").write_text("x", encoding="utf-8")
    (pack / "prompts" / "routing_disambiguation_hints.txt").write_text("x", encoding="utf-8")

    schema = {
        "type": "OBJECT",
        "properties": {
            "recommended_template": {
                "type": "STRING",
                "enum": ["research-article", "review-or-perspective"],
            },
        },
    }
    (pack / "schemas" / "document_profile.vertex.schema.json").write_text(
        json.dumps(schema), encoding="utf-8"
    )
    (pack / "schemas" / "structured_note.vertex.schema.json").write_text("{}", encoding="utf-8")
    (pack / "templates" / "_domain_quality_rules.txt").write_text("x", encoding="utf-8")
    (pack / "templates" / "research-article.txt").write_text("x", encoding="utf-8")
    (pack / "templates" / "review-or-perspective.txt").write_text("x", encoding="utf-8")
    (pack / "config" / "model_routing_policy.json").write_text("{}", encoding="utf-8")
    return pack


def test_validate_pack_passes_for_complete_pack(fresh_pack):
    ok, errors = bdp.validate_pack(fresh_pack)
    assert ok is True
    assert errors == []


def test_validate_pack_fails_when_required_file_missing(fresh_pack):
    (fresh_pack / "prompts" / "seed_terms_guidance.txt").unlink()
    ok, errors = bdp.validate_pack(fresh_pack)
    assert ok is False
    assert any("seed_terms_guidance.txt" in e for e in errors)


def test_validate_pack_fails_when_enum_lacks_template_file(fresh_pack):
    """Enum 'review-or-perspective' references a file that we delete."""
    (fresh_pack / "templates" / "review-or-perspective.txt").unlink()
    ok, errors = bdp.validate_pack(fresh_pack)
    assert ok is False
    assert any("review-or-perspective" in e for e in errors)


def test_validate_pack_fails_when_template_file_not_in_enum(fresh_pack):
    """Template file exists but no enum value references it (orphan)."""
    (fresh_pack / "templates" / "orphan-template.txt").write_text("x", encoding="utf-8")
    ok, errors = bdp.validate_pack(fresh_pack)
    assert ok is False
    assert any("orphan-template" in e for e in errors)


def test_validate_pack_fails_for_missing_directory(tmp_path):
    ok, errors = bdp.validate_pack(tmp_path / "does-not-exist")
    assert ok is False
    assert any("missing" in e.lower() for e in errors)


def test_validate_pack_ignores_underscore_prefixed_files(fresh_pack):
    """`_domain_quality_rules.txt` shouldn't be reported as orphan."""
    ok, errors = bdp.validate_pack(fresh_pack)
    # The underscore-prefix file is correctly ignored — pack passes.
    assert ok is True
    assert errors == []


def test_slugify():
    assert bdp._slugify("Cell Biology") == "cell-biology"
    assert bdp._slugify("CS / ML") == "cs-ml"
    assert bdp._slugify("ABC123") == "abc123"
    assert bdp._slugify("  trim me  ") == "trim-me"
    assert bdp._slugify("") == ""


def test_validate_real_catalysis_pack():
    """Sanity check against the actual catalysis pack in this repo."""
    repo_root = Path(__file__).resolve().parent.parent
    catalysis = repo_root / "domain-packs" / "catalysis"
    if not catalysis.exists():
        pytest.skip("catalysis pack not present in this checkout")
    ok, errors = bdp.validate_pack(catalysis)
    assert ok is True, f"catalysis pack should validate: {errors}"
