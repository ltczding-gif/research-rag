"""Smoke tests for scanner/note_render.py.

Covers the YAML colon bug (the TODO at the top of gemini_analyze_pdf.py),
field ordering stability, abstract section injection, filename extraction
fallback, and the validation report.
"""

from __future__ import annotations

import re

import pytest
import yaml

from note_render import (
    build_multifacet_frontmatter,
    build_multifacet_validation_report,
    render_multifacet_note,
    resolve_multifacet_generated_name,
)


def _minimal_draft(**overrides):
    """Build the minimum note_draft dict for rendering."""
    fm = {
        "title_en": "Test Title",
        "title_zh": "测试标题",
        "authors": ["Smith"],
        "year": 2024,
        "journal": "Nature",
        "doi": "10.1000/xyz",
        "keywords": ["a", "b"],
        "topic": ["topic1"],
        "research_domain": "electrochemistry",
        "document_type": "research-article",
        "note_template": "electrocatalysis-experimental",
        "seed_terms": ["term1"],
        "scope_hint": "core",
        "signal_quality": "strong",
        "routing_confidence": "high",
    }
    fm.update(overrides.pop("frontmatter", {}))
    body = overrides.pop("body_markdown", "# 文献基本信息\n\nbody content\n\n# 客观摘要\nstuff")
    return {"frontmatter": fm, "body_markdown": body}


# --- YAML quoting & round-trip -------------------------------------------


def test_yaml_quote_special_chars_journal_with_colon():
    """The bug at the top of gemini_analyze_pdf.py:
    journal = 'Applied Catalysis B: Environmental' must round-trip clean
    through yaml.safe_load. The colon must be quoted, not naively appended."""
    draft = _minimal_draft(frontmatter={"journal": "Applied Catalysis B: Environmental"})
    fm_block = build_multifacet_frontmatter(
        note_draft=draft,
        pdf_paths=["/tmp/paper.pdf"],
        combined_hash="a" * 64,
    )
    # Strip the ``---`` fences that build_multifacet_frontmatter wraps with
    inner = re.sub(r"^---\n|\n---$", "", fm_block.strip())
    parsed = yaml.safe_load(inner)
    assert parsed["journal"] == "Applied Catalysis B: Environmental"


def test_yaml_quote_value_with_hash():
    draft = _minimal_draft(frontmatter={"title_en": "C-H Activation # Mini-Review"})
    fm_block = build_multifacet_frontmatter(
        note_draft=draft, pdf_paths=["/tmp/x.pdf"], combined_hash="h",
    )
    inner = re.sub(r"^---\n|\n---$", "", fm_block.strip())
    parsed = yaml.safe_load(inner)
    assert parsed["title_en"] == "C-H Activation # Mini-Review"


def test_yaml_quote_single_quote_in_value():
    draft = _minimal_draft(frontmatter={"title_zh": "用户's catalyst"})
    fm_block = build_multifacet_frontmatter(
        note_draft=draft, pdf_paths=["/tmp/x.pdf"], combined_hash="h",
    )
    inner = re.sub(r"^---\n|\n---$", "", fm_block.strip())
    parsed = yaml.safe_load(inner)
    assert parsed["title_zh"] == "用户's catalyst"


def test_yaml_field_ordering_stable():
    """Frontmatter fields must appear in the documented fixed order
    regardless of the input dict's insertion order."""
    draft = _minimal_draft()
    # Shuffle the dict
    fm = draft["frontmatter"]
    shuffled = dict(reversed(list(fm.items())))
    draft["frontmatter"] = shuffled

    fm_block = build_multifacet_frontmatter(
        note_draft=draft, pdf_paths=["/tmp/x.pdf"], combined_hash="h",
    )
    # title_en should appear before title_zh, year before journal, etc.
    title_en_pos = fm_block.index("title_en:")
    title_zh_pos = fm_block.index("title_zh:")
    year_pos = fm_block.index("year:")
    journal_pos = fm_block.index("journal:")
    assert title_en_pos < title_zh_pos < year_pos < journal_pos


def test_pdf_path_fields_appear_in_order():
    draft = _minimal_draft()
    fm_block = build_multifacet_frontmatter(
        note_draft=draft,
        pdf_paths=["/tmp/main.pdf", "/tmp/si.pdf"],
        combined_hash="h",
    )
    assert "pdf_0_name: main.pdf" in fm_block
    assert "pdf_1_name: si.pdf" in fm_block
    assert fm_block.index("pdf_0_name") < fm_block.index("pdf_1_name")


def test_zotero_parent_key_appears_when_provided():
    draft = _minimal_draft()
    fm_block = build_multifacet_frontmatter(
        note_draft=draft,
        pdf_paths=["/tmp/x.pdf"],
        combined_hash="h",
        zotero_parent_key="ABCD1234",
    )
    assert "zotero_parent_key: ABCD1234" in fm_block


def test_zotero_parent_key_omitted_when_none():
    draft = _minimal_draft()
    fm_block = build_multifacet_frontmatter(
        note_draft=draft, pdf_paths=["/tmp/x.pdf"], combined_hash="h",
    )
    assert "zotero_parent_key" not in fm_block


# --- legacy_combined_hash -------------------------------------------------


def test_legacy_hash_emitted_when_differs_from_combined_hash():
    """Two-PDF group whose path-sort vs hash-sort orders disagree: both
    fields must appear so a wiped ledger can rebuild without losing the
    legacy lookup."""
    draft = _minimal_draft()
    fm_block = build_multifacet_frontmatter(
        note_draft=draft,
        pdf_paths=["/tmp/main.pdf", "/tmp/si.pdf"],
        combined_hash="stable" + "0" * 58,
        legacy_combined_hash="legacy" + "0" * 58,
    )
    assert f"combined_hash: stable{'0' * 58}" in fm_block
    assert f"legacy_combined_hash: legacy{'0' * 58}" in fm_block
    # Order: combined_hash before legacy_combined_hash before pdf_*
    assert fm_block.index("combined_hash:") < fm_block.index("legacy_combined_hash:")
    assert fm_block.index("legacy_combined_hash:") < fm_block.index("pdf_0_name:")


def test_legacy_hash_omitted_when_equals_combined_hash():
    """Single-PDF case: legacy and stable are equal by construction;
    don't double the field."""
    draft = _minimal_draft()
    fm_block = build_multifacet_frontmatter(
        note_draft=draft,
        pdf_paths=["/tmp/x.pdf"],
        combined_hash="abc",
        legacy_combined_hash="abc",
    )
    assert "combined_hash: abc" in fm_block
    assert "legacy_combined_hash" not in fm_block


def test_legacy_hash_omitted_when_none():
    """Default behavior (no legacy_combined_hash arg) must not regress."""
    draft = _minimal_draft()
    fm_block = build_multifacet_frontmatter(
        note_draft=draft,
        pdf_paths=["/tmp/x.pdf"],
        combined_hash="abc",
    )
    assert "legacy_combined_hash" not in fm_block


def test_tagging_shell_fields_always_empty():
    """Generated notes must ship with empty tags / candidate_tags_*; the
    prefill step fills those later."""
    draft = _minimal_draft()
    fm_block = build_multifacet_frontmatter(
        note_draft=draft, pdf_paths=["/tmp/x.pdf"], combined_hash="h",
    )
    assert "tags: []" in fm_block
    assert "candidate_tags_high: []" in fm_block
    assert "human_reviewed: 0" in fm_block


# --- render_multifacet_note end-to-end -----------------------------------


def test_render_with_no_abstract_no_injection():
    draft = _minimal_draft()
    rendered = render_multifacet_note(
        note_draft=draft, pdf_paths=["/tmp/x.pdf"], combined_hash="h",
    )
    assert "## 英文摘要原文" not in rendered
    assert rendered.endswith("\n")
    assert "body content" in rendered


def test_render_with_abstract_injects_after_bibliography():
    draft = _minimal_draft()
    rendered = render_multifacet_note(
        note_draft=draft,
        pdf_paths=["/tmp/x.pdf"],
        combined_hash="h",
        zotero_abstract="This paper investigates X using Y.",
    )
    assert "## 英文摘要原文" in rendered
    assert "This paper investigates X using Y." in rendered
    # Abstract section should appear after 文献基本信息 but before 客观摘要
    abstract_pos = rendered.index("## 英文摘要原文")
    bib_pos = rendered.index("# 文献基本信息")
    summary_pos = rendered.index("# 客观摘要")
    assert bib_pos < abstract_pos < summary_pos


def test_render_with_abstract_idempotent():
    """If the body already contains the abstract section, don't add a second copy."""
    draft = _minimal_draft(body_markdown=(
        "# 文献基本信息\n\nbib\n\n## 英文摘要原文\nexisting abstract\n\n# 客观摘要\nx"
    ))
    rendered = render_multifacet_note(
        note_draft=draft,
        pdf_paths=["/tmp/x.pdf"],
        combined_hash="h",
        zotero_abstract="should not be added",
    )
    assert rendered.count("## 英文摘要原文") == 1
    assert "should not be added" not in rendered


# --- Filename resolution -------------------------------------------------


def test_resolve_generated_name_recommended():
    draft = _minimal_draft(body_markdown=(
        "# 文献基本信息\n\n推荐保存文件名: 2024-Nature-Smith-CO2RR\n\n# more"
    ))
    name = resolve_multifacet_generated_name(draft, ["/tmp/some_paper.pdf"])
    assert name == "2024-Nature-Smith-CO2RR_review_note.md"


def test_resolve_generated_name_chinese_colon():
    draft = _minimal_draft(body_markdown=(
        "# 文献基本信息\n\n推荐保存文件名：2024-FullWidth-Colon\n\n# more"
    ))
    name = resolve_multifacet_generated_name(draft, ["/tmp/x.pdf"])
    assert name == "2024-FullWidth-Colon_review_note.md"


def test_resolve_generated_name_fallback_to_pdf_basename():
    draft = _minimal_draft(body_markdown="# 文献基本信息\n\nno recommendation here")
    name = resolve_multifacet_generated_name(draft, ["/tmp/2024_Smith_paper.pdf"])
    assert name == "2024_Smith_paper_review_note.md"


def test_resolve_generated_name_strips_path_chars():
    draft = _minimal_draft(body_markdown=(
        "# 文献基本信息\n\n推荐保存文件名: bad/file*name?<>"
    ))
    name = resolve_multifacet_generated_name(draft, ["/tmp/x.pdf"])
    # All filesystem-illegal characters must be stripped
    assert "/" not in name and "*" not in name and "?" not in name
    assert "<" not in name and ">" not in name


# --- Validation report ---------------------------------------------------


def test_validation_canary_ready_for_normal_note():
    draft = _minimal_draft()
    rendered = render_multifacet_note(
        note_draft=draft, pdf_paths=["/tmp/x.pdf"], combined_hash="h",
    )
    report = build_multifacet_validation_report(rendered)
    assert report["frontmatter_present"] is True
    assert report["body_present"] is True
    assert report["forbidden_hits"] == []
    assert report["canary_ready"] is True


def test_validation_catches_forbidden_field():
    rendered = "---\ntitle_en: x\ntag_review_status: pending\n---\n\nbody"
    report = build_multifacet_validation_report(rendered)
    assert "tag_review_status" in report["forbidden_hits"]
    assert report["canary_ready"] is False


def test_validation_no_body():
    rendered = "---\ntitle_en: x\n---\n\n"
    report = build_multifacet_validation_report(rendered)
    assert report["body_present"] is False
    assert report["canary_ready"] is False
