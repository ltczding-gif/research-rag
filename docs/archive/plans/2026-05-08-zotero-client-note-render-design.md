# Design: `zotero_client` + `note_render` extractions

**Date:** 2026-05-08
**Status:** Approved (Option B from `docs/architecture-discussion-2026-05-08.md`)
**Scope:** Tier-1 + Tier-2 refactors 1 and 2 of 4 from the architecture roadmap. The remaining two (`vault_index` and `PipelineRequest/Result` dataclasses) are deferred until these have settled empirically.

## Background

After 7 polish commits cleaned up the surface (`--pipeline-mode legacy` removal, backend factory centralization, hash unification, sub-agent flow, smoke tests, three bug fixes), an architecture discussion with a sub-agent surfaced 5 module-deepening candidates. The user picked the two with the highest leverage-to-risk ratio:

1. **`scanner/zotero_client.py`** — consolidate the 3-way duplicated Zotero SQL.
2. **`scanner/note_render.py`** — pure transformation extraction from `gemini_analyze_pdf.py`.

Three other candidates (vault_index, PipelineRequest dataclass, PostPublishPlan) are deferred. Two anti-candidates (backend factory, `_hashing.py`) explicitly will not be touched.

## Constraints

- **Scope is `$REPO_ROOT/` only.** The user's local `.localrag\`, `.agents\skills\`, and `.claude\skills\` are unchanged. This matches all 7 prior commits' contract.
- Each extraction is its own commit with its own prep test landing first or in the same commit.
- No interface change to the orchestrator. `run_multifacet_spec_pipeline` keeps its 17-kwarg signature for now (that's commit 4 of the original roadmap, deferred).
- `service/build_pdf_db.py:get_parent_key_by_pdf_path` should also be migrated to `zotero_client` for consistency, even though `service/` and `scanner/` are deployed independently.

## Commit 1 — `scanner/zotero_client.py`

### What moves

| From | To |
|---|---|
| `scanner/gemini_analyze_pdf.py:get_parent_key` (~60 lines) | `scanner/zotero_client.py:get_parent_key` |
| `service/build_pdf_db.py:get_parent_key_by_pdf_path` (~52 lines) | imports from `scanner/zotero_client.py` (or duplicate with parity test, mirroring the `_hashing.py` pattern) |
| `scanner/zotero_batch_scanner.py` SQL inside `get_zotero_pdf_groups` | the SQL stays inline for now; `get_zotero_pdf_groups` is its own concern (path resolution, prefs.js parsing, fan-out). Don't conflate. |

### Public interface

```python
# scanner/zotero_client.py
def get_parent_key(pdf_path: str | Path, *, zotero_db: str | Path | None = None) -> str | None:
    """Look up the Zotero parent item key for a PDF path.

    Two strategies, in order:
      1. Storage-key extraction from .../storage/<KEY>/<filename> path (exact match).
      2. Filename LIKE substring fallback (linked-file mode; collision-prone).

    Returns the parent key, or None if neither matches.
    """
```

That's the entire public surface — one function. Both call sites become one-line imports.

### Service-side handling

`service/` and `scanner/` are deployable independently. Three options:

- **A. Direct import**: `service/build_pdf_db.py` does `sys.path.insert(0, .../scanner/)` then `from zotero_client import get_parent_key`. Couples deployment.
- **B. Duplicate with parity test**: like `_hashing.py` / `service/build_pdf_db.py:get_combined_hash` today. New `tests/test_zotero_client_parity.py` asserts both implementations produce identical results.
- **C. Move to a shared `common/` directory**: rejected in the discussion as overkill for current scope.

**Pick B.** Same pattern as `_hashing.py`, predictable for future maintainers.

### Pre-test

`tests/test_zotero_client.py`:
- `test_get_parent_key_storage_path`: in-memory SQLite fixture with `items` + `itemAttachments` tables, query a path matching `storage/<KEY>/foo.pdf`, assert correct parent key returned.
- `test_get_parent_key_like_fallback`: same fixture, but query a linked-file-style path; assert LIKE fallback fires.
- `test_get_parent_key_no_match`: query a nonexistent path; assert `None`.

`tests/test_zotero_client_parity.py`:
- `test_scanner_and_service_get_parent_key_agree`: AST-extract `service/build_pdf_db.py:get_parent_key_by_pdf_path` (mirroring the existing `test_hash_parity.py` pattern), compare both implementations against a fixture DB.

### Risk

S — pure read path, no behavior change, no callers broken. Riskiest line: the regex extraction `[A-Za-z0-9]{8}` was widened in the prior commit; that's the boundary case we're consolidating, not introducing.

## Commit 2 — `scanner/note_render.py`

### What moves

From `scanner/gemini_analyze_pdf.py`:

| Function | Lines (approx) | Notes |
|---|---|---|
| `_yaml_quote_if_needed` | ~25 | YAML special-char quoting |
| `_render_yaml_field` | ~30 | YAML field serialization with ordering |
| `_sanitize_seed_terms` | ~40 | Hallucination guard against seed_terms |
| `build_multifacet_frontmatter` | ~60 | Fixed-order frontmatter assembly |
| `_inject_section_after_bibliography` | ~30 | Abstract section position rule |
| `extract_recommended_filename` | ~25 | Pull "推荐保存文件名" line out of body |
| `normalize_recommended_filename` | ~20 | Filename FS-safety |
| `render_multifacet_note` | ~40 | Top-level: dict → markdown string |
| `build_multifacet_validation_report` | ~30 | Validate rendered note structure |

Total: ~300 lines.

### Public interface

```python
# scanner/note_render.py

def render(
    *,
    note_draft: dict,
    pdf_paths: list[str],
    combined_hash: str,
    zotero_parent_key: str | None,
    zotero_abstract: str | None = None,
) -> str:
    """Render a structured note draft into a Markdown string with YAML frontmatter."""

def extract_filename(rendered_note: str, *, fallback_pdf_path: str) -> str:
    """Pull the recommended filename out of the rendered body, falling back to the PDF basename."""

def validate(rendered_note: str) -> dict:
    """Validation report for a rendered note (frontmatter present, body present, no forbidden fields)."""
```

Three exported functions, all pure. No filesystem, no model client, no subprocess. The internal helpers (`_yaml_quote_if_needed`, `_sanitize_seed_terms`, etc.) become private to the module.

### What stays in `gemini_analyze_pdf.py`

- `write_multifacet_output_note` — wraps `render` + filesystem IO. Keep the IO seam at the orchestrator level for now.
- `find_existing_multifacet_note_matches`, `_iter_live_vault_note_paths`, `LIVE_VAULT_EXCLUDED_*` — these are vault-scanning, not note-rendering. They go to `vault_index` in commit 3 (deferred batch).
- `resolve_multifacet_publish_path` — borderline, but it depends on `find_existing_multifacet_note_matches`, so it goes with `vault_index`.

### Pre-test

`tests/test_note_render.py`:
- `test_yaml_quote_special_chars_journal_with_colon`: feed `note_draft` with `journal = "Applied Catalysis B: Environmental"`. Render. Assert `yaml.safe_load(rendered_frontmatter)["journal"] == "Applied Catalysis B: Environmental"`. (This is the bug at line 1 of `gemini_analyze_pdf.py` TODO.)
- `test_yaml_field_ordering_stable`: assert frontmatter fields appear in the documented fixed order regardless of input dict order.
- `test_sanitize_seed_terms_drops_unanchored`: feed `seed_terms` containing strings not present in `title_en` / `keywords` / `topic`; assert dropped.
- `test_extract_filename_recommended`: feed body with "推荐保存文件名: 2024-Foo.md"; assert correct return.
- `test_extract_filename_fallback`: feed body without that line; assert PDF-basename fallback.

### Risk

M — larger diff than commit 1. Risk areas:
1. The fixed YAML field ordering must be preserved exactly (any reorder breaks downstream `build_notes_db.py` metadata extraction).
2. `_sanitize_seed_terms` reads `title_en` / `title_zh` / `keywords` / `topic` from the draft — must not lose the anchoring logic.
3. `extract_recommended_filename` regex must match both Chinese-colon (`：`) and English-colon (`:`) variants.

## Order of operations within each commit

1. Add the new module file.
2. Land the prep tests.
3. Update call sites (one-line imports in most cases).
4. Delete the inlined original code.
5. Run smoke suite (`pytest tests/`) — must stay green.
6. Single commit with diff scoped to: new module + tests + call-site updates + deletions.

## What this design does NOT touch

- `gemini_analyze_pdf.py:run_multifacet_spec_pipeline` signature
- `gemini_analyze_pdf.py` model routing functions (`resolve_profiler_model`, `resolve_note_generator_model`)
- `gemini_analyze_pdf.py` post-publish actions
- Any backend module (`scanner/backends/`)
- Any service-side script other than `service/build_pdf_db.py`'s parent-key lookup
- `query_server.py`
- Any SKILL file
- Any of the user's local `$HOME\` files

## Success criteria

After both commits land:

- `gemini_analyze_pdf.py` line count drops by ~360 (60 from commit 1, ~300 from commit 2)
- `pytest tests/` shows ≥27 tests passing (was 19; +3 zotero_client tests + 1 zotero parity + 5 note_render tests = +9)
- No behavior change visible at any CLI invocation
- Both new modules are importable in isolation without triggering `extract_pdf_groups_from_notes` side effects
- The YAML colon-bug TODO at `gemini_analyze_pdf.py:1` is resolved (covered by `test_yaml_quote_special_chars_journal_with_colon`)
