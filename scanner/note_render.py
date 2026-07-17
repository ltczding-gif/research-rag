"""
Pure note rendering: turn a validated note_draft dict into a Markdown
string with YAML frontmatter.

This module is **pure transformation** — no filesystem, no model client,
no SQL. The caller fetches any Zotero metadata (e.g. the abstract)
upstream and passes it in. That isolation lets us test the rendering
logic with no fixtures beyond a dict.

Public surface (4 functions):

  build_multifacet_frontmatter(note_draft, pdf_paths, combined_hash, zotero_parent_key=None)
      → str   (YAML block, ``---\\n...\\n---``)

  resolve_multifacet_generated_name(note_draft, pdf_paths)
      → str   (filename ending in ``_review_note.md``)

  render_multifacet_note(note_draft, pdf_paths, combined_hash, zotero_parent_key=None, zotero_abstract="")
      → str   (full note: frontmatter + body)

  build_multifacet_validation_report(rendered_note)
      → dict  (frontmatter_present, body_present, forbidden_hits, canary_ready)

Internal helpers (single underscore prefix) cover YAML quoting, abstract
text normalization, bibliography section injection, filename extraction
and normalization. They are not part of the public API.

Frontmatter field ordering is fixed and must not be changed without
coordinating with `service/build_notes_db.py`'s metadata extraction.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# YAML serialization helpers
# ---------------------------------------------------------------------------


def _yaml_quote_if_needed(value):
    if value is None:
        return "''"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if text == "":
        return "''"
    special_chars = [":", "#", "{", "}", "[", "]", ",", "&", "*", "?", "|", "<", ">", "=", "!", "%", "@", "`"]
    if text.isdigit():
        return text
    if any(ch in text for ch in special_chars) or "\\" in text:
        return "'" + text.replace("'", "''") + "'"
    return text


def _render_yaml_field(key, value):
    if isinstance(value, list):
        if not value:
            return [f"{key}: []"]
        lines = [f"{key}:"]
        for item in value:
            lines.append(f"  - {_yaml_quote_if_needed(item)}")
        return lines
    return [f"{key}: {_yaml_quote_if_needed(value)}"]


# ---------------------------------------------------------------------------
# Frontmatter (public)
# ---------------------------------------------------------------------------

# Fixed-order frontmatter fields. Keep in lock-step with what
# service/build_notes_db.py expects to read; reordering breaks downstream
# metadata extraction.
_FRONTMATTER_FIELD_ORDER = [
    "title_en",
    "title_zh",
    "authors",
    "year",
    "journal",
    "doi",
    "keywords",
    "topic",
    "research_domain",
    "document_type",
    "note_template",
    "seed_terms",
    "scope_hint",
    "signal_quality",
    "routing_confidence",
]


def build_multifacet_frontmatter(
    note_draft,
    pdf_paths,
    combined_hash,
    zotero_parent_key=None,
    legacy_combined_hash=None,
):
    """Render the YAML frontmatter block for a structured note draft.

    Returns a string starting with ``---`` and ending with ``---``,
    containing the canonical fixed-order field set plus per-PDF path
    fields and tagging-shell fields.

    `legacy_combined_hash` is emitted **only when it differs from
    `combined_hash`** — the common single-PDF case has them equal, and
    we don't want to bloat the frontmatter for no reason. The two
    diverge only for multi-PDF groups whose path-sorted and hash-sorted
    file orders disagree (a small minority of pre-2026-03 notes).
    Recording it lets the dedup index recover from a wiped ledger
    without losing pre-2026-03 lookups.

    **Tagging shell fields are always emitted as their default empty
    values** (``tags: []``, ``candidate_tags_high: []``, etc.,
    ``human_reviewed: 0``), regardless of whether `note_draft` carries
    them. The contract is: generated notes ship with empty tags; the
    `prefill_candidate_tags` post-publish step (run later) fills in
    `candidate_tags_*`; humans set `tags` and `human_reviewed`. Anything
    the model emitted in those fields is silently discarded here.
    """
    frontmatter = note_draft["frontmatter"]
    lines = ["---"]
    for field in _FRONTMATTER_FIELD_ORDER:
        if field in frontmatter:
            lines.extend(_render_yaml_field(field, frontmatter[field]))
    lines.extend(_render_yaml_field("combined_hash", combined_hash))
    if legacy_combined_hash and legacy_combined_hash != combined_hash:
        lines.extend(_render_yaml_field("legacy_combined_hash", legacy_combined_hash))
    for idx, path in enumerate(pdf_paths):
        lines.extend(_render_yaml_field(f"pdf_{idx}_name", os.path.basename(path)))
        lines.extend(_render_yaml_field(f"pdf_{idx}_path", os.path.abspath(path)))
    if zotero_parent_key:
        lines.extend(_render_yaml_field("zotero_parent_key", zotero_parent_key))
    lines.extend(_render_yaml_field("tags", []))
    lines.extend(_render_yaml_field("candidate_tags_high", []))
    lines.extend(_render_yaml_field("candidate_tags_medium", []))
    lines.extend(_render_yaml_field("candidate_tags_low", []))
    lines.extend(_render_yaml_field("human_reviewed", 0))
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# English abstract text normalization (private helpers)
# ---------------------------------------------------------------------------

_ABSTRACT_STANDALONE_FRAGMENT_RE = re.compile(
    r"^\s*(?:[0-9]+|[+−–\-]|[+−–\-][0-9]+|[0-9]+[+−–\-])\s*$",
    re.MULTILINE,
)


def _normalize_english_abstract_text(abstract_text):
    text = str(abstract_text or "").replace("\r\n", "\n").replace("\r", "\n")
    for old in (" ", " ", " ", " "):
        text = text.replace(old, " ")
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    if lines and re.fullmatch(r"(?i)abstract[:.]?", lines[0]):
        lines.pop(0)
        while lines and not lines[0]:
            lines.pop(0)

    output = ""
    for line in lines:
        if not line:
            continue
        if not output:
            output = line
        elif _ABSTRACT_STANDALONE_FRAGMENT_RE.match(line):
            output += line
        elif re.match(r"^[,.;:)]", line):
            output += line
        elif output.endswith(("-", "‐", "‑", "‒", "–")) and line[:1].islower():
            output += line
        else:
            output += " " + line

    formula_prefixes = (
        "CO|COO|COOH|CH|H|O|N|NO|C|OH|Cu|CuO|SO|HCO|KHCO|NaHCO|HCOO|"
        "C2H|CH3|H2SO|H2O|OCCO|sp"
    )
    element_prefixes = "Li|Na|K|Rb|Cs|H|O|OH|Cl|Cu|Ag|Au|Pt|Pd|Ni|Fe|Co|Zn|Sn|Pb"
    output = re.sub(rf"(?i)\b({formula_prefixes})\s+([0-9]+)\b", r"\1\2", output)
    output = re.sub(rf"(?i)\b({element_prefixes})\s*([+−\-])\b", r"\1\2", output)
    output = re.sub(r"\b(C[0-9]+)\s*\+\b", r"\1+", output)
    output = re.sub(r"\b(CO[0-9]+)\s+RR\b", r"\1RR", output)
    output = re.sub(r"\b(CO[0-9]+)\s+R\b", r"\1R", output)
    output = re.sub(r"\b(cm|m|s|A|mA|mol|g|mg)\s+([−\-][0-9]+)\b", r"\1\2", output)
    output = re.sub(r"\bC\s*[–\-]\s*C\b", "C-C", output)
    output = re.sub(r"(?<=[a-z0-9)])\.,\s+(?=[A-Z])", ". ", output)
    output = re.sub(r"\s+([,.;:%)])", r"\1", output)
    output = re.sub(r"([(])\s+", r"\1", output)
    output = re.sub(r"\s{2,}", " ", output)
    return output.strip()


def _build_english_abstract_section(abstract_text):
    abstract = _normalize_english_abstract_text(abstract_text)
    if not abstract:
        return ""
    return "## 英文摘要原文\n" + abstract


# ---------------------------------------------------------------------------
# Bibliography section injection
# ---------------------------------------------------------------------------


def _inject_section_after_bibliography(body_markdown, section_markdown):
    """Insert `section_markdown` immediately after the ``# 文献基本信息``
    block (heading + content), and before the next section.

    Idempotent: if the body already contains ``## 英文摘要原文``, returns
    the body unchanged. If no bibliography section is found, prepends.
    """
    body = str(body_markdown or "").strip()
    section = str(section_markdown or "").strip()
    if not section:
        return body
    if "## 英文摘要原文" in body:
        return body
    bibliography_match = re.search(r"(?m)^#{1,2}\s*文献基本信息\s*$", body)
    if not bibliography_match:
        if not body:
            return section
        return section + "\n\n" + body

    remainder = body[bibliography_match.end():]
    next_heading_match = re.search(r"(?m)^#{1,2}\s+", remainder)
    if not next_heading_match:
        return body.rstrip() + "\n\n" + section

    insert_at = bibliography_match.end() + next_heading_match.start()
    before = body[:insert_at].rstrip()
    after = body[insert_at:].lstrip()
    return before + "\n\n" + section + "\n\n" + after


# ---------------------------------------------------------------------------
# Filename extraction & normalization (private)
# ---------------------------------------------------------------------------


def _extract_recommended_filename(body_markdown):
    """Pull the value out of a `推荐保存文件名: <value>` line in the body.

    Tolerates the Chinese full-width colon (``：``), various leading
    Markdown noise (backticks, asterisks, blockquotes), and surrounding
    quote marks. Returns None if no valid line was found.
    """
    body = str(body_markdown or "")
    for raw_line in body.splitlines():
        if "推荐保存文件名" not in raw_line:
            continue
        if ":" not in raw_line and "：" not in raw_line:
            continue
        _label, value = re.split(r"[:：]", raw_line, maxsplit=1)
        value = re.sub(r"^[\s`*_>\-\+]+", "", value).strip()
        value = value.strip("`*_\"' ")
        if value:
            return value
    return None


def _normalize_recommended_filename(raw_name, fallback_stem):
    """Sanitize and standardize a recommended filename.

    - Strips filesystem-illegal characters (Windows-safe).
    - Normalizes various dash variants to a plain hyphen.
    - Collapses whitespace and repeated separators.
    - Ensures the stem ends in ``_review_note`` (with an underscore, not a hyphen).
    - Adds the ``.md`` extension.

    If `raw_name` is empty after sanitization, falls back to `fallback_stem`.
    """
    candidate = str(raw_name or "").strip()
    if not candidate:
        candidate = fallback_stem

    candidate = os.path.basename(candidate)
    candidate = (
        candidate.replace("—", "-")
        .replace("–", "-")
        .replace("－", "-")
        .replace("‑", "-")
        .replace("‒", "-")
        .replace("―", "-")
    )
    candidate = re.sub(r"[\r\n\t]", " ", candidate)
    candidate = re.sub(r"[`*\"']", "", candidate).strip()
    candidate = "".join(c for c in candidate if c not in r'\/:*?"<>|').strip()
    candidate = re.sub(r"\s+", " ", candidate)
    candidate = re.sub(r"\s*-\s*", "-", candidate)
    candidate = re.sub(r"\s*_\s*", "_", candidate)
    candidate = re.sub(r"-{2,}", "-", candidate)
    candidate = re.sub(r"_{2,}", "_", candidate)

    if not candidate:
        candidate = fallback_stem

    suffix = Path(candidate).suffix.lower()
    if suffix:
        candidate = candidate[: -len(suffix)]

    candidate = candidate.strip(" .-_")
    if not candidate:
        candidate = fallback_stem

    if re.search(r"(^|[_-])review_note$", candidate, re.IGNORECASE):
        candidate = re.sub(r"[-]review_note$", "_review_note", candidate, flags=re.IGNORECASE)
    else:
        candidate = f"{candidate}_review_note"

    return f"{candidate}.md"


# ---------------------------------------------------------------------------
# Public API: name resolution + rendering + validation
# ---------------------------------------------------------------------------


def resolve_multifacet_generated_name(note_draft, pdf_paths):
    """Resolve the output filename for a rendered note.

    Looks for a ``推荐保存文件名: <value>`` line in `note_draft.body_markdown`;
    falls back to ``<pdf_basename>_review_note`` derived from the first PDF.
    Always returns a name ending in ``.md``.
    """
    fallback_stem = f"{Path(pdf_paths[0]).stem}_review_note"
    raw_name = _extract_recommended_filename(note_draft.get("body_markdown", ""))
    return _normalize_recommended_filename(raw_name, fallback_stem)


def render_multifacet_note(
    note_draft,
    pdf_paths,
    combined_hash,
    zotero_parent_key: Optional[str] = None,
    zotero_abstract: str = "",
    legacy_combined_hash: Optional[str] = None,
):
    """Render a structured note draft into a single Markdown string.

    The caller fetches `zotero_abstract` upstream (e.g. via
    `scanner/zotero_client.py:get_zotero_abstract_note`) and passes it
    in. This module does no SQL of its own.

    `legacy_combined_hash` is forwarded to `build_multifacet_frontmatter`,
    which emits it only when it differs from `combined_hash`.
    """
    frontmatter = build_multifacet_frontmatter(
        note_draft=note_draft,
        pdf_paths=pdf_paths,
        combined_hash=combined_hash,
        zotero_parent_key=zotero_parent_key,
        legacy_combined_hash=legacy_combined_hash,
    )
    body_markdown = note_draft.get("body_markdown", "").strip()
    english_abstract_section = _build_english_abstract_section(zotero_abstract)
    body_markdown = _inject_section_after_bibliography(body_markdown, english_abstract_section)
    if body_markdown:
        return frontmatter + "\n\n" + body_markdown + "\n"
    return frontmatter + "\n"


def build_multifacet_validation_report(rendered_note):
    """Pure structural validation of a rendered note.

    Returns a dict with `frontmatter_present`, `body_present`,
    `forbidden_hits` (list of fields that should never appear in a
    finalized note), and `canary_ready` (composite boolean).
    """
    forbidden_fields = [
        "tag_review_status:",
        "candidate_needed:",
        "candidate_needed_raw_terms:",
        "routing_evidence:",
        "warnings:",
        "body_evidence_targets:",
    ]
    forbidden_hits = [field.rstrip(":") for field in forbidden_fields if field in rendered_note]
    frontmatter_present = rendered_note.startswith("---\n") and "\n---\n" in rendered_note
    body_present = "\n---\n\n" in rendered_note and bool(rendered_note.split("\n---\n\n", 1)[1].strip())
    return {
        "frontmatter_present": frontmatter_present,
        "body_present": body_present,
        "forbidden_hits": forbidden_hits,
        "canary_ready": frontmatter_present and body_present and not forbidden_hits,
    }


__all__ = [
    "build_multifacet_frontmatter",
    "resolve_multifacet_generated_name",
    "render_multifacet_note",
    "build_multifacet_validation_report",
]
