# Architecture friction — fresh-eyes pass (2026-05-08)

## TL;DR

The highest-leverage opportunity is extracting a **VaultNote module** from
`gemini_analyze_pdf.py`: the ~450 lines covering frontmatter rendering, YAML
quoting, publish-path resolution, and vault-scanning logic have nothing to do
with LLM invocation and belong in a module with a small interface hiding large
implementation. Right behind it is a **ZoteroClient** extraction: the same
two-strategy SQL lookup (`storage/<KEY>/` path first, LIKE fallback) is copied
verbatim into three files with no shared home, making any fix or test a
three-location edit. The backend factory is the one place that has already
earned its deep-module status; it should be left alone.

---

## Candidates (ranked by leverage)

### 1. VaultNote — "the 450-line note assembly cluster living inside the orchestrator"

- **Cluster**: `scanner/gemini_analyze_pdf.py` lines 746–1210 — YAML quoting
  helpers (`_yaml_quote_if_needed`, `_render_yaml_field`), frontmatter builder
  (`build_multifacet_frontmatter`), abstract-section injector
  (`_inject_section_after_bibliography`), filename normalization
  (`extract_recommended_filename`, `normalize_recommended_filename`), vault
  scanner (`_iter_live_vault_note_paths`, `_read_note_frontmatter_mapping`,
  `FRONTMATTER_BLOCK_RE`, `LIVE_VAULT_EXCLUDED_*`), publish-path resolution
  (`resolve_multifacet_publish_path`, `find_existing_multifacet_note_matches`),
  and the full `render_multifacet_note` + `write_multifacet_output_note` call
  chain.

- **Friction observed**: Reading the orchestration function
  `run_multifacet_spec_pipeline` (lines 1496–1626) requires mentally holding
  two concerns at once: the two-stage LLM pipeline and the note assembly
  mechanics. The actual LLM calls (`run_document_profiler`,
  `run_note_generator`) are 10 lines total; the surrounding note machinery —
  rendering YAML, scanning the vault for existing notes, normalizing filenames,
  injecting the abstract section — is 600+ lines. When I asked "where does the
  rendered note come from?" I had to jump between `render_multifacet_note`,
  `build_multifacet_frontmatter`, `_yaml_quote_if_needed`, and
  `_render_yaml_field` before I had a coherent answer. The complexity is real,
  but it's all hidden inside the same 2000-line file as the LLM dispatch.

- **What's currently exposed vs what should be hidden**: Currently everything
  is a flat namespace — callers of `render_multifacet_note` must also know
  about `build_multifacet_frontmatter`, `_sanitize_seed_terms`, the YAML field
  ordering list, the abstract section injection rule. A `VaultNote` module
  would expose one function: `render(note_draft, pdf_paths, combined_hash,
  zotero_parent_key) -> str` and one entry point: `write(rendered_note,
  generated_name, output_root, *, combined_hash, ...) -> Path`. The
  30-something-line YAML quoting machinery, the vault-scan loop, the filename
  normalization regex chain — all hidden. Callers need only the 2-function
  surface.

- **Dependency category**: **pure-internal**. The vault scanner reads `.md`
  files from disk and parses YAML frontmatter; `render` is pure transformation
  of dicts-to-string. No external API, no subprocess, no model call. This makes
  it extremely cheap to test in isolation.

- **Test impact**: Today there are zero tests for frontmatter rendering,
  filename normalization, YAML quoting edge cases (the TODO at line 1 of
  `gemini_analyze_pdf.py` about colons in journal titles is exactly this), or
  the vault-scan / publish-path resolution logic. A `VaultNote` module with a
  small interface enables:
  - `test_render_frontmatter_special_chars`: feed a note_draft with
    `journal = "Applied Catalysis B: Environmental"`, assert the YAML round-
    trips clean through `yaml.safe_load`.
  - `test_publish_path_prefers_existing_name_match`: give a vault with one
    existing note at a mismatched filename, assert `write()` lands at the
    existing path not a new one.
  - `test_abstract_injection_position`: assert the abstract section appears
    after `## 文献基本信息` and before the next heading.
  
  All three are currently untestable without standing up the full orchestrator.

- **Refactor cost**: **M**. The logic is already cleanly separated in the file
  (it doesn't interleave with LLM calls). The riskiest bit is that
  `find_existing_multifacet_note_matches` reaches into `DEFAULT_VAULT_ROOT` as
  a module-level default — you must be careful to pass it explicitly in the new
  API rather than baking in the global.

---

### 2. ZoteroClient — "the same SQL written three times in three files"

- **Cluster**: `scanner/gemini_analyze_pdf.py:get_parent_key` (lines 1628–
  1688), `service/build_pdf_db.py:get_parent_key_by_pdf_path` (lines 36–88),
  and implicitly `scanner/zotero_batch_scanner.py:get_zotero_pdf_groups` (lines
  295–405, which contains a third copy of the Zotero SQL attachment join).

- **Friction observed**: `get_parent_key` in `gemini_analyze_pdf.py` and
  `get_parent_key_by_pdf_path` in `build_pdf_db.py` are the same function with
  the same two-strategy algorithm, the same SQL, and different variable names.
  The docstring in `build_pdf_db.py` explicitly says "Mirrors
  `scanner/gemini_analyze_pdf.py:get_parent_key`". This is a "KEEP IN SYNC"
  comment waiting to become a bug. The vault-scanner adds a third SQL join
  against the same Zotero schema. Any change to the Zotero DB schema (which has
  happened in the past) requires finding all three sites.

- **What's currently exposed vs what should be hidden**: A `ZoteroClient`
  module would expose: `get_parent_key(pdf_path) -> str | None`,
  `get_pdf_groups(data_dir, *, base_dir, since) -> list[list[str]]`. All the
  SQLite connection management, the copy-to-temp workaround for the open-Zotero
  lock risk, the storage-path regex, the LIKE fallback, the prefs.js traversal
  for `baseAttachmentPath` — hidden. Callers just ask "what's the parent key
  for this PDF."

- **Dependency category**: **leaf-IO**. All it does is read SQLite and the
  filesystem. No network, no LLM, no subprocess. 

- **Test impact**: Currently untested. A `ZoteroClient` module enables:
  - `test_get_parent_key_storage_path`: give a path matching
    `storage/<KEY>/filename.pdf`, assert the key lookup returns the right parent.
  - `test_get_parent_key_like_fallback`: give a path that doesn't match the
    storage layout, assert the LIKE fallback fires and returns None when not
    found.
  - Both can run against a tiny in-memory SQLite fixture — no real Zotero DB
    needed.

- **Refactor cost**: **S**. It's a clean extraction with no shared state. The
  one non-obvious dependency is the `_zotero_is_running()` check in
  `zotero_batch_scanner.py`, which is scanner-specific policy rather than
  Zotero client logic — keep that in the scanner, not in the client.

---

### 3. NoteIndex / VaultScanner — "the O(n) vault walk hidden in a dedup check"

- **Cluster**: `scanner/gemini_analyze_pdf.py` and `scanner/zotero_batch_scanner.py`
  each independently define:
  - `FRONTMATTER_BLOCK_RE` — identical regex in both
  - `_read_note_frontmatter_mapping` — identical function in both
  - `_iter_live_vault_note_paths` — identical generator in both, but with one
    meaningful difference: `gemini_analyze_pdf.py` includes `".claude"` in
    `LIVE_VAULT_EXCLUDED_PATH_PARTS`; the migrate script (`migrate_combined_hash_to_stable.py`)
    does not. A third copy lives there too.

- **Friction observed**: The divergence in exclusion sets is already a latent
  bug: a `.claude/` note would be found by `zotero_batch_scanner.py`'s vault
  walk (which excludes `.claude`) but not by `gemini_analyze_pdf.py`'s. The
  exclusion sets are not the same. This is the kind of bug that's impossible to
  notice from either file in isolation — it only appears when you put them side
  by side. Beyond the bug: `build_live_note_index` in `zotero_batch_scanner.py`
  builds a full in-memory index of the vault (hash → paths), then passes it
  through subprocess to `gemini_analyze_pdf.py` via a temp JSON file, which
  re-normalizes and re-validates it in `normalize_note_index_payload`. The
  serialization round-trip exists because they can't share code.

- **What's currently exposed vs what should be hidden**: A single `vault_index`
  module would expose `build_index(vault_root) -> dict` and
  `find_matches(index, *, combined_hash, zotero_parent_key) -> list[Path]`.
  The glob walk, exclusion filtering, frontmatter YAML parsing, dedup logic —
  hidden. The temp-file serialization round-trip becomes unnecessary: the
  scanner builds the index in-process and passes it directly to the analyze
  function.

- **Dependency category**: **pure-internal** (just reads `.md` files). Lower
  integration risk than ZoteroClient; the serialization round-trip elimination
  would actually shrink code.

- **Test impact**: 
  - `test_vault_excludes_progress_dirs`: a vault tree with `progress/gate_backups/x_review_note.md` should not appear in the index.
  - `test_vault_excludes_claude_dir`: a `.claude/x_review_note.md` should not appear.
  - These would catch the current `.claude` exclusion divergence immediately.

- **Refactor cost**: **S** for the extraction, **M** for the subprocess
  serialization elimination (requires wiring `gemini_analyze_pdf.py` to accept
  an index dict rather than just a file path). The file-path API is load-bearing
  for the `--note-index-file` CLI flag used by the scanner.

---

### 4. PostPublishPlan — "a command-list builder that's not testable at its seam"

- **Cluster**: `scanner/gemini_analyze_pdf.py:build_post_publish_plan` (lines
  1267–1382), `run_post_publish_workflow` (lines 1421–1465),
  `should_trigger_kimi_fallback` (lines 1391–1418), and the parallel duplicate
  in `scanner/zotero_batch_scanner.py:parse_batch_post_publish_actions` /
  `split_batch_post_publish_actions` (lines 152–189). Five functions across two
  files, two of which duplicate logic.

- **Friction observed**: `build_post_publish_plan` returns a list of command
  dicts (with hardcoded PowerShell strings). `run_post_publish_workflow` calls
  `subprocess.run` on them. The `kimi_fallback` skip logic lives inside
  `run_post_publish_workflow` and reads the note file from disk mid-execution.
  This means the only way to test "does `kimi_fallback` get skipped when tags
  are already populated?" is to create a note on disk with the right frontmatter
  and actually call `run_post_publish_workflow` — or mock `subprocess.run`. The
  interesting logic (`should_trigger_kimi_fallback`) is buried inside an
  imperative loop that also handles retries, captures stdout, and writes results.
  Also: `parse_batch_post_publish_actions` in `zotero_batch_scanner.py`
  duplicates `parse_post_publish_actions` in `gemini_analyze_pdf.py` with the
  same allowed tokens and alias map, but without the `ValueError` on unknown
  actions.

- **What's currently exposed vs what should be hidden**: `build_post_publish_plan`
  already separates the "what commands" concern from the "run them" concern.
  The gap: `should_trigger_kimi_fallback` should be part of the plan phase, not
  injected mid-execution inside `run_post_publish_workflow`. A cleaner interface:
  `resolve_actions(note_path, requested_actions) -> list[str]` (applies
  `kimi_fallback` skip logic eagerly, returns the final action list), then
  `build_plan(actions, note_path, ...) -> list[dict]` (pure, no IO), then
  `execute_plan(plan, runner=subprocess.run)`.

- **Dependency category**: **adapter-needed**. The PowerShell restart command
  (lines 1358–1366) is a hardcoded Windows-specific string. Any cross-platform
  intent is already fiction; a thin adapter layer that abstracts "restart query
  server" is the right seam.

- **Test impact**: With `build_plan` pure:
  - `test_plan_excludes_kimi_when_tags_populated`: build a note with high+medium
    candidates, call `resolve_actions`, assert `kimi_fallback` is not in result.
  - `test_plan_includes_review_queue_only_at_batch_end`: assert the batch
    splitting logic produces the right per-item and batch-end action lists.

- **Refactor cost**: **S–M**. The logic is already partially separated.
  Biggest risk: `should_trigger_kimi_fallback` reads the note from disk —
  callers must be updated to pass note content or a parsed frontmatter dict
  rather than a path.

---

### 5. query_server.py QueryLog — "a log formatter embedded in an HTTP handler"

- **Cluster**: `service/query_server.py` lines 139–465 — ~325 lines of query
  log logic (rendering frontmatter YAML, rendering search plan/runs/notes/papers
  sections, building filenames, loading/saving the registry, idempotency check)
  are inline in the same module as the ChromaDB search endpoints.

- **Friction observed**: The `write_query_log` endpoint handler (lines 765–886)
  is 120 lines long and does: validation, normalization, idempotency check,
  filename generation, full Markdown rendering, file write, registry update, and
  response construction. The render functions (`render_frontmatter`,
  `render_search_plan_section`, `render_search_runs_section`,
  `render_notes_hits_section`, `render_paper_hits_section`,
  `render_query_log_markdown`) are pure functions but live in the same file as
  the Flask globals (`pdf_col`, `notes_col`, `chroma_ready`). Any time you want
  to unit-test "does the search plan section render correctly?" you have to
  import the whole Flask app and its ChromaDB initialization path (or set
  `LOCALRAG_SKIP_CHROMA_INIT=1`).

- **What's currently exposed vs what should be hidden**: Move the query log
  machinery to `service/query_log.py`. The Flask handlers in `query_server.py`
  become 10-line wrappers: validate, call `query_log.write(data)`, return JSON.
  The rendering and registry logic is hidden behind `write()` and `append()`.

- **Dependency category**: **pure-internal** (pure string transforms + file
  IO). The only external dependency is `yaml.safe_dump`, which is already
  imported.

- **Test impact**: 
  - `test_render_frontmatter_preserves_key_order`: assert the YAML keys come
    out in the documented order.
  - `test_write_query_log_idempotency`: call `write()` twice with the same
    `idempotency_key`, assert only one file is created.
  - Currently both require standing up Flask.

- **Refactor cost**: **S**. Pure extraction, no logic change. Lowest risk of
  the five candidates. The main reason it ranks last: `query_server.py` already
  has good function-level decomposition; the friction is in the file boundary,
  not in how the functions are written.

---

## Anti-candidates

### The backend factory (`scanner/backends/`)

At first glance the factory looks like boilerplate: a `make_backend()` switch
and a `make_backend_from_env()` switch with nearly identical branching. You
might be tempted to collapse them into a registry dict. Don't. The two-function
design earns its keep because env-var resolution is inherently per-backend
(Vertex needs project-ID resolution from credentials JSON; subagent needs no
env vars at all). The separation between "low-level constructor" and "high-level
env resolution" is real: tests can call `make_backend("subagent")` directly
without touching the environment. The duplicated `_resolve_project_id_from_credentials`
between `backends/__init__.py` and `gemini_analyze_pdf.py` is the one rough
edge here, but it's small and the docstring explains why it exists (no circular
import). Collapsing the factory into a registry would make adding a backend
harder, not easier.

### `scanner/_hashing.py`

This module is already the right shape for Ousterhout's deep-module ideal:
small interface (5 exported functions), large hidden implementation (two hash
algorithms, canonical path normalization, legacy compatibility). The tests in
`test_hashing.py` exercise it cleanly at the boundary. The `KEEP IN SYNC`
comment in `build_pdf_db.py` is an irritant, but `test_hash_parity.py` already
catches drift. The right fix is a shared package (not splitting `_hashing.py`
further). Don't refactor the module itself.

---

## What I'd test next

1. **Frontmatter YAML round-trip**: Write `test_yaml_quote_special_chars` in
   `tests/`. Feed `build_multifacet_frontmatter` a `note_draft` where
   `frontmatter["journal"] = "Applied Catalysis B: Environmental"` (the exact
   field mentioned in the TODO at line 1 of `gemini_analyze_pdf.py`). Assert
   `yaml.safe_load(rendered_frontmatter)["journal"]` equals the original string.
   This test has no external dependencies and would land before VaultNote
   extraction as a safety net.

2. **Vault exclusion consistency**: Write `test_vault_exclusions_are_consistent`
   that imports `LIVE_VAULT_EXCLUDED_PATH_PARTS` from both
   `gemini_analyze_pdf.py` and `zotero_batch_scanner.py` and asserts equality.
   This is a one-line test that would catch the current `.claude` divergence and
   any future drift.

3. **Parent-key lookup with tiny SQLite fixture**: Write
   `test_get_parent_key_storage_path(tmp_path)` that creates a 3-table SQLite
   DB (`items`, `itemAttachments`, `itemData`) with one row each, then calls
   `get_parent_key` from `gemini_analyze_pdf.py` with a path matching
   `storage/<KEY>/foo.pdf`. Assert it returns the correct parent key. This test
   would immediately expose the duplicated SQL in `build_pdf_db.py` and apply
   pressure toward the ZoteroClient extraction.
