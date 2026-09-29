# Code Review: backend-pluggability commit (e8f6da4..5560d92)

**Reviewer:** Claude Code (Sonnet 4.6)
**Date:** 2026-05-08
**Repo:** `$REPO_ROOT` (staging)
**Commit:** `5560d92` — Add pluggable processor backends (Vertex / Gemini API / Anthropic / sub-agent)
**Diff size:** +998 / -148 across 14 files

---

## Strengths

**Clean ABC design (`scanner/backends/base.py`)**
The two-method split (`attach_pdfs` + `call_model`) is the right abstraction boundary. PDF transport (GCS upload, inline bytes, base64 blocks, manifest file) varies wildly across providers; model invocation takes a provider-neutral triple of `(system_prompt, user_prompt, schema)`. Keeping these as separate lifecycle calls, with `attach_pdfs` acting as a per-paper setup hook, lets orchestration code stay completely ignorant of provider internals. The default no-op in `base.py:38` for `attach_pdfs` is a sensible convenience so simple subclasses only override what they need.

**Lazy imports done consistently (`scanner/backends/__init__.py:14-31`)**
All four backends are imported inside the `if name == ...` branches of `make_backend()`. This means users without `google-genai` installed can run `--backend anthropic` without hitting an import error at startup, and vice versa. The pattern is uniform across all four backends and the error messages from each `__init__` are actionable (they spell out the pip command).

**Anthropic tool-use schema approach (`scanner/backends/anthropic_api.py:95-140`)**
Using forced tool-use (`tool_choice={"type": "tool", "name": _TOOL_NAME}`) to enforce structured output is the correct Anthropic SDK pattern. The JSON schema feeds directly into `input_schema`, and `block.input` on a `tool_use` response block is already a parsed dict — so there is no JSON-decoding ambiguity on the happy path.

**`SubagentManifestPending` sentinel (`scanner/backends/base.py:72-93`)**
Raising a typed exception instead of returning a sentinel value or `None` is the right design choice. The exception carries `manifest_path` and `run_dir` as structured attributes, not just a message string, so the orchestrator can act on them programmatically rather than parsing text.

**Archive manifest lifecycle preserved for Vertex (`scanner/gemini_analyze_pdf.py`)**
The refactored `main()` correctly writes the archive manifest at three lifecycle points for the Vertex path: after `attach_pdfs` (status: uploaded), after a successful pipeline (status: completed), and in the `except Exception` handler (status: generation_failed). The `is_vertex` guard prevents this GCS-specific bookkeeping from crashing on non-Vertex backends.

**Documentation is genuinely useful (`skills/gemini-literature-processor/SKILL.md`)**
The backend selection table (line ~55) is clear and correct. The sub-agent dispatch instructions tell the user exactly what prompt to give the Task tool. The `.env.example` per-backend credential blocks with mutual exclusivity comments make first-time setup straightforward.

---

## Issues

### Critical (Must Fix)

#### C1 — `NameError: bucket` in legacy path end-of-function write
**File:** `scanner/gemini_analyze_pdf.py:2334`
**Severity:** Crash — the legacy pipeline mode (`--pipeline-mode legacy --backend vertex`) will `NameError` at the very end of `main()` when it tries to write the completion manifest.

**What's wrong:** The old code built a local `bucket` object via `ensure_bucket()`. The refactor moved GCS setup into `VertexBackend.attach_pdfs()`, so `bucket` is no longer a local variable in `main()`. All the in-try-block writes correctly use `backend._bucket`, but the *end-of-function* call at line 2334 still uses the bare name `bucket`:

```python
write_archive_manifest(bucket, combined_hash, archive_manifest)   # NameError
```

This only fires after a successful legacy run (the note is already saved to disk), but the hash is not appended to the ledger until *after* this line (line 2337), so the paper will be treated as unprocessed on the next batch scan. Combined with a crash, this is a silent double-processing risk.

**How to fix:** Replace `bucket` with `backend._bucket` (or, better, expose a public `@property gcs_bucket` on `VertexBackend` to avoid the private access).

#### C2 — Top-level unconditional `from google import genai` breaks non-Vertex invocations
**File:** `scanner/gemini_analyze_pdf.py:20-21`
**Severity:** Import error at startup for any user who has not installed `google-genai` but wants `--backend anthropic` or `--backend subagent`.

**What's wrong:** Lines 20-21 unconditionally import:
```python
from google import genai
from google.genai import types
```
The rest of the file only uses `types` in: (a) `upload_pdfs_to_gcs()` — dead code (see C3), (b) `create_vertex_clients()` — dead code, and (c) the legacy pipeline path's `types.GenerateContentConfig` at line 2165. An `anthropic` or `subagent` user will fail immediately on import, before they even parse CLI args, making the backend selection feature unusable without a full Vertex dep stack.

**How to fix:** Guard the `types.GenerateContentConfig` usage inside the legacy path behind `if is_vertex:` (which is already checked at line 2151). Then move the `from google import genai; from google.genai import types` imports to inside `create_vertex_clients()`, mirroring the lazy-import pattern used in the backends package. Or simply delete the dead legacy functions and the import becomes unnecessary.

#### C3 — `create_vertex_clients()` and `upload_pdfs_to_gcs()` are dead code that masks C2
**File:** `scanner/gemini_analyze_pdf.py:1830-1968`
**Severity:** Dead code that silently forces a google-genai dependency and prevents the lazy-import design from working.

**What's wrong:** Both `create_vertex_clients()` (line 1830) and `upload_pdfs_to_gcs()` (line 1924) are defined but **never called** anywhere in the refactored file. The Vertex work is now fully inside `VertexBackend.attach_pdfs()`. The comment on `create_vertex_clients` says "Legacy helper kept for the legacy --pipeline-mode legacy path", but the legacy path uses `backend.client.models.generate_content` and `backend._parts` — it does not call `create_vertex_clients`. The functions are truly dead.

The `upload_pdfs_to_gcs` function duplicates the GCS upload logic that now lives in `VertexBackend.attach_pdfs`, with slightly different behavior (it calls `types.Part.from_uri` unconditionally). Keeping both around guarantees drift.

**How to fix:** Delete both functions. The top-level `from google import genai; from google.genai import types` imports go with them (resolving C2 naturally). If the legacy pipeline mode is kept, the backend object already has `.client` and `._parts` available; it does not need its own genai client.

---

### Important (Should Fix)

#### I1 — Sub-agent flow only emits Stage A manifest, then exits; Stage B is never reached
**File:** `scanner/backends/subagent.py:call_model` / `scanner/gemini_analyze_pdf.py:run_multifacet_spec_pipeline`
**What's wrong:** In `run_multifacet_spec_pipeline`, Stage A (`run_document_profiler`) calls `backend.call_model(stage="profiler", ...)`. `SubagentBackend.call_model` writes `manifest-profiler.json` and raises `SubagentManifestPending`. This propagates up through `run_document_profiler`, through `run_multifacet_spec_pipeline`, and is caught by `main()`. **The function returns immediately.** Stage B (`run_note_generator`) is never called and `manifest-note_generator.json` is never written.

The SKILL.md says "The current build emits a manifest per stage (one for profiler, one for note_generator)." This is inaccurate — only the profiler manifest is emitted per run; Stage B requires a second invocation after the sub-agent completes Stage A output. The user currently has no clear path to produce Stage B's manifest without Stage A's `document_profile` output, since that JSON is the input to `build_note_generator_user_prompt`.

This means: in its current state, the sub-agent mode **cannot produce a note at all**, even with a sub-agent that correctly fills Stage A. It is a design stub, not a functional path. This should be clearly flagged in SKILL.md and STATUS.md as "Stage A only" or "manifest-per-stage requires sequential invocation", and the current wording that implies both manifests emit in one run should be corrected.

#### I2 — Legacy pipeline path leaks private attributes (`backend._parts`, `backend.client`, `backend._bucket`)
**Files:** `scanner/gemini_analyze_pdf.py:2161, 2163, 2096, 2145, 2186`
**What's wrong:** The legacy path accesses `backend._parts` and `backend.client` directly (lines 2161, 2163). The Vertex archive manifest writes access `backend._bucket` (lines 2096, 2145, 2186). These are private attrs defined on `VertexBackend` with no public API in `ProcessorBackend`. The code is guarded by `is_vertex` (correct), but is still a leaky abstraction: `_bucket` is not a public property, so changing its name or storage strategy in `VertexBackend` silently breaks the orchestrator.

`VertexBackend` already exposes an `archived_files` property and a `bucket_name` property. The missing piece is a `gcs_bucket` property for the bucket object itself. The `_parts` access in the legacy path bypasses the abstraction entirely.

**How to fix:** Add `@property gcs_bucket(self)` to `VertexBackend` returning `self._bucket`. Replace all `backend._bucket` accesses in `main()` with `backend.gcs_bucket`. For the legacy path's `backend._parts` access, either keep it behind a clear `# VertexBackend-only` comment or add `@property parts(self)` to the Vertex class.

#### I3 — `_translate_model_id` substring match is fragile under Gemini versioned model strings
**File:** `scanner/backends/anthropic_api.py:75-89`
**What's wrong:** The translation checks `"pro" in m` first. This is correct for `"gemini-2.5-pro"` → sonnet mapping. However, if the orchestrator passes a versioned string like `"models/gemini-2.5-pro-preview-06-05"` or any future model name containing `pro` as a non-indicator substring, the mapping still fires and returns sonnet — which is the intended behavior for that specific case. The real risk is the reverse: if the user passes an explicit **Claude** model id that happens to contain the substring `pro` (e.g. a hypothetical `"claude-pro-something"` or `"claude-3-5-opus-20250101"` which does *not* contain 'pro'), the logic falls through to the final `return model_id` (correct). No current Claude model ids contain "pro" as a substring, so this is low risk today.

The more genuine issue: if a user sets `--model claude-opus-4-7` (which does not contain "pro"), the `_translate_model_id` returns `"claude-opus-4-7"` verbatim — which is correct, and the comment says so. But there is no validation that this is a real Anthropic model id. An unknown model passed through will get an API error from Anthropic with no helpful message, because the function assumes "if it doesn't match our keywords, it's an explicit user override."

**How to fix:** Document the fallthrough behavior in the docstring with a concrete example. Optionally add a prefix check: `if model_id.startswith("claude-")` before the fallthrough return, and raise a `ValueError` for unrecognized strings to catch typos earlier.

#### I4 — `--pipeline-mode` defaults to `"legacy"` even when `--backend` is not `vertex`
**File:** `scanner/gemini_analyze_pdf.py:482-485`
**What's wrong:** The argparser defaults `--pipeline-mode` to `"legacy"`. For non-Vertex backends, the legacy path is blocked with an explicit error at line 2151-2157 (`sys.exit(1)`). This means a first-time `anthropic` or `gemini-api` user who forgets to add `--pipeline-mode multifacet-spec` gets a hard exit with an error message that says "use `--pipeline-mode multifacet-spec`" — a friction point that is entirely avoidable.

**How to fix:** Change the default to `"multifacet-spec"` (the recommended path for all backends), or derive the default dynamically based on `args.backend` in a post-parse step. Add a deprecation notice on `"legacy"` in the help string.

#### I5 — `--resume` is undocumented as "not implemented" in one key place
**File:** `skills/gemini-literature-processor/SKILL.md` (sub-agent section)
**What's wrong:** The SKILL.md sub-agent section ends with: "a `--resume <run_dir>` path will pick them up and finalize the note (still on the roadmap; for now the manifest mode is best for a single-paper validation, not a 100-paper batch)". STATUS.md Phase 7 also says "a `--resume` finalize path is on the roadmap". This is adequately flagged in the docs.

However, the `main()` handler for `SubagentManifestPending` (line 2171-2179) also says: "After the sub-agent has finished, re-invoke the scanner (a --resume flow is on the roadmap)." There is no `--resume` flag in the argparser — so if a user follows that instruction literally they get a confusing argparse error. The message should instead say: "At this time, `--resume` is not yet implemented. Watch the project for updates."

---

### Minor (Nice to Have)

#### M1 — Fallback JSON parse in `AnthropicBackend.call_model` can mask schema violations
**File:** `scanner/backends/anthropic_api.py:126-134`
**What:** When forced tool-use returns no `tool_use` block (which should not happen with `tool_choice={"type": "tool", ...}`, but might with API version drift), the backend attempts to parse JSON from a text block. This can return a dict that does not conform to `schema`. Callers pass the result to `validate_document_profile` / `validate_note_draft`, so the violation will surface eventually — but the error message will point to the validator, not the API response.

**Suggestion:** Log a `WARNING` before the fallback JSON parse: `"tool_use block missing; attempting text fallback — this may indicate an API or schema version mismatch."` Keep the fallback but make it visible.

#### M2 — `SubagentBackend.call_model` does not include `document_profile` in Stage B manifest
**File:** `scanner/backends/subagent.py:84-116`
**What:** Stage B (`note_generator`) calls `build_note_generator_user_prompt(document_profile=..., ...)` *before* calling `backend.call_model`. So the `user_prompt` passed to `call_model` already bakes in the document profile data. The manifest therefore contains the full rendered user prompt — including the profile — and the sub-agent has everything it needs. This is actually correct behavior. But the manifest `response_schema` for Stage B is `structured_note.vertex.schema.json`, which is a large schema. It would be useful to include the schema name/version as a human-readable field alongside `response_schema` so the sub-agent prompt can reference it.

#### M3 — `GeminiAPIBackend` has no inline PDF size guard
**File:** `scanner/backends/gemini_api.py:attach_pdfs`
**What:** `VertexBackend` does not include a size guard either (it trusts GCS), but the original `gemini_analyze_pdf.py` had pre-flight size/page checks (`VERTEX_PDF_MAX_SIZE_BYTES`, `VERTEX_PDF_MAX_PAGES`). These checks presumably still run before `attach_pdfs` is called. However, the Gemini direct API has its own inline byte limits (currently 20MB for inline base64; the Vertex path via GCS is 50MB+). If the pre-flight uses the Vertex limits, users hitting the Gemini API with 30-50MB PDFs will get cryptic API errors. Worth adding an explicit check or a warning in `GeminiAPIBackend.attach_pdfs` for files over 20MB.

#### M4 — `make_backend_from_args` duplicates the backend name routing that `make_backend` already does
**File:** `scanner/gemini_analyze_pdf.py:1854-1907`
**What:** `make_backend_from_args` has its own `if name == "vertex" / "gemini-api" / "anthropic" / "subagent"` dispatch, then calls `make_backend(name, ...)` passing resolved kwargs. This means the list of valid backend names appears in two places (`argparser.choices` + this dispatch). Adding a new backend currently requires edits in three files: the backends `__init__.py`, the `make_backend_from_args` function, and the argparser choices. Consider centralizing the "what env vars does each backend need" logic closer to the backend class (e.g., a `@classmethod from_env(cls)` factory on each backend class), which `make_backend_from_args` can delegate to.

#### M5 — Inconsistency: `gemini-api` vs `gemini_api` normalization only in `make_backend_from_args`, not in `make_backend`
**File:** `scanner/gemini_analyze_pdf.py:1873` vs `scanner/backends/__init__.py:22`
**What:** `make_backend_from_args` accepts `"gemini_api"` (underscore) and normalizes it to `"gemini-api"`. `make_backend()` in `__init__.py` accepts `"gemini-api"` and `"gemini"` as aliases but not `"gemini_api"`. If someone calls `make_backend("gemini_api", ...)` directly, they get a `ValueError`. The normalization should live in `make_backend()` (which does `name.replace("_", "-")`), but `__init__.py:16` already does `name = (name or "vertex").lower().replace("_", "-")`. So the `__init__.py` actually handles this correctly. The issue is that `make_backend_from_args` has a redundant `"gemini_api"` check at line 1873 that is never reached if the user passes `gemini_api` via CLI (argparser `choices` rejects it before `make_backend_from_args` runs). This is dead code that creates a false impression of support for underscore spelling on the CLI.

---

## Recommendations

1. **Fix C1 immediately** (`bucket` NameError at line 2334). This is a one-line fix: `write_archive_manifest(backend._bucket, combined_hash, archive_manifest)`. Or add a `gcs_bucket` property to `VertexBackend` first (see I2).

2. **Fix C2 and C3 together**: delete `create_vertex_clients()` and `upload_pdfs_to_gcs()` (both dead), which removes the justification for the unconditional `from google import genai; from google.genai import types` imports at the top of the file. Move any remaining `types.GenerateContentConfig` usage inside the `if is_vertex:` guard. This resolves the startup import failure for non-Vertex users.

3. **Correct the sub-agent SKILL.md claim** (I1): change "emits a manifest per stage (one for profiler, one for note_generator)" to "emits one manifest (profiler stage only) per invocation; Stage B requires a separate run after the sub-agent completes Stage A and the user manually re-invokes". Or implement a two-phase run (Stage A emit → wait → Stage B emit) if that is the intended UX.

4. **Expose `gcs_bucket` as a public property on `VertexBackend`** (I2) to remove all `backend._bucket` accesses from `main()`. This is a 2-line change in `vertex.py` and a 3-line change in `gemini_analyze_pdf.py`.

5. **Change `--pipeline-mode` default to `"multifacet-spec"`** (I4). The legacy path is vertex-only, deprecated by the user's own commit message, and is a trap for non-Vertex users. Making `multifacet-spec` the default removes a common failure mode.

---

## Assessment: No — Do Not Merge (Critical fixes required)

The abstraction design is sound and most of the implementation is well-executed. However, **two critical bugs block merging**:

- **C1** (`bucket` NameError): The Vertex legacy pipeline mode will crash at the end of every successful run, causing missed ledger writes and re-processing on the next batch scan.
- **C2/C3** (unconditional google-genai import): Any user running `--backend anthropic` or `--backend subagent` without the `google-genai` package hits a startup `ImportError`, making the entire multi-backend feature non-functional for them from the first invocation.

Fix C1 (1 line), C2+C3 (delete ~140 lines of dead code and guard one import), and correct the sub-agent documentation (I1). The important-tier issues can follow in a subsequent commit. After those fixes, the commit is mergeable.

**Critical issues:** 3 (C1, C2, C3 grouped)
**Important issues:** 5 (I1–I5)
