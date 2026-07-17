# Note Generation Pipeline: Investigation Report

**Scope:** Gemini + Zotero note generation pipeline  
**Sources:** `skills/gemini-literature-processor\`  
**Date:** 2026-05-08

---

## 1. Data Flow Diagram

```
Zotero SQLite ($ZOTERO_DB_PATH)
        │
        │  [copied to temp] zotero_batch_scanner.py
        │  SQL: itemAttachments JOIN items WHERE path LIKE '%.pdf'
        │  Resolves: storage: → zotero_data/storage/<attachKey>/
        │            attachments: → $ZOTERO_ATTACHMENT_BASE_DIR\...
        │            absolute paths passed through
        ▼
PDF Groups [[main.pdf], [main.pdf, SI.pdf], ...]
        │
        │  combined_hash computed (SHA-256 of sorted per-file SHA-256s)
        │  prefilter: skip if hash in processed_history.txt
        │             skip if hash/parent_key found in live_note_index
        ▼
gemini_analyze_pdf.py (one subprocess per group)
        │
        │  1. PDF preflight (pypdf): page count, size, split if > 50 MB / 1000 pages
        │  2. Upload prepared PDFs to GCS
        │     gs://<bucket>/pdf-inputs/<combined_hash>/<idx>_<safename>.pdf
        │
        │  [multifacet-spec pipeline]
        │
        │  Stage A: Document Profiler (always gemini-2.5-flash)
        │    System: document_profiler.system.txt
        │    Schema: document_profile.vertex.schema.json
        │    → research_domain, document_type, recommended_template,
        │      routing_confidence, is_review_like, is_multichapter_thesis
        │
        │  Stage B: Model routing decision
        │    Flash if primary_pdf_pages < 30 AND total_pdf_pages < 60
        │    Pro if ≥ 30 pages primary, OR document_type in
        │      [textbook, phd-dissertation, review, perspective, commentary],
        │      OR is_review_like=true, OR is_multichapter_thesis=true
        │
        │  Stage C: Note Generator (flash or gemini-2.5-pro)
        │    System: note_generator.system.txt
        │    Template rules: template_rules/<recommended_template>.txt
        │                  + template_rules/_shared_rules.txt
        │    Schema: structured_note.vertex.schema.json
        │    → frontmatter fields, body_markdown, section_diagnostics,
        │      adapter_signals
        │
        │  Render: build_multifacet_frontmatter() adds:
        │    combined_hash, pdf_N_name, pdf_N_path, zotero_parent_key,
        │    tags:[], candidate_tags_high:[], ..., human_reviewed: 0
        │  Inject: Zotero abstractNote pulled via second SQLite query,
        │    appended as "## 英文摘要原文" section
        ▼
Rendered Markdown note (frontmatter + body)
        │
        │  Validation: build_multifacet_validation_report()
        │    checks: frontmatter_present, body_present, forbidden_fields absent
        │
        │  Write to: canary dir (pipeline_reports/…/canary_notes/)
        │          OR live vault ($LOCALRAG_NOTES_DIR/) when --publish-target vault
        │  Filename: extracted from "推荐保存文件名" line in body,
        │            normalized, suffixed _review_note.md
        │
        │  Post-publish actions (vault only, default: prefill + review_queue):
        │    prefill → prefill_candidate_tags.py
        │    kimi_fallback → run_tagging_pipeline.ps1 (if tags sparse)
        │    review_queue → export_review_queue.py
        ▼
processed_history.txt  ← append combined_hash (one hex per line)
        │
        │  (optional downstream, outside this pipeline)
        ▼
LocalRAG rebuild: build_notes_db.py + build_pdf_db.py → query_server.py
```

---

## 2. Components and Responsibilities

**Scanner (`zotero_batch_scanner.py`)**  
Copies Zotero's SQLite to a temp file to avoid file-lock conflicts, queries all PDF attachments grouped by parent item, resolves the three Zotero path schemes (`storage:`, `attachments:`, absolute), normalizes groups by content-hash to deduplicate, loads `processed_history.txt` and a live-vault JSON index to prefilter already-processed groups, then fans out to `gemini_analyze_pdf.py` subprocesses — sequentially for ≤5 items, with a 3-worker `ThreadPoolExecutor` for larger batches. Handles retry (3 attempts) with backoff for 429 and other transient errors, and marks `non_retryable_error` codes to stop immediately on corrupt/missing/oversize PDFs.

**Single-PDF Analyzer (`gemini_analyze_pdf.py`)**  
Orchestrates the full Gemini pipeline for one PDF group. Computes `combined_hash`, checks ledger and live-vault index for duplicates, runs PDF preflight (page count + size via pypdf), splits oversized PDFs into Vertex-safe chunks, uploads prepared PDFs to GCS, drives the two-stage Gemini API sequence (Document Profiler then Note Generator), renders the final Markdown note, writes it to the target directory, and appends the hash to `processed_history.txt`. Also performs optional post-publish actions.

**Prompt Assembly (inside `gemini_analyze_pdf.py`)**  
Each Gemini call is constructed as `list(pdf_parts) + [user_prompt_string]` passed to `client.models.generate_content()` with `response_mime_type="application/json"`, `response_schema=<schema>`, and `temperature=0.0`. The user prompt for the Note Generator embeds the full document profile JSON, the `recommended_template` ID, and the combined template rules text. System prompts are loaded from `prompts/*.system.txt` files.

**Schema Validation (inside `gemini_analyze_pdf.py`)**  
The function `_validate_against_schema()` performs a recursive Python-side check of the Gemini response against the Vertex schema JSON before the response is used. Required fields are verified, enum values checked, and type assertions made. After validation, `_sanitize_seed_terms()` grounds `seed_terms` against `title_en`, `title_zh`, `keywords`, and `topic` fields to prevent hallucinated terms from surviving.

**Template Rules Selector**  
`run_note_generator()` reads `document_profile["recommended_template"]` (set by the Document Profiler) and loads `template_rules/<recommended_template>.txt` plus `template_rules/_shared_rules.txt`. These two files are concatenated and injected into the Note Generator user prompt as writing instructions. The seven available templates are: `electrocatalysis-experimental`, `thermocatalysis-experimental`, `review-or-perspective`, `phd-dissertation`, `methods-or-materials-synthesis`, `foundational-theory`, `generic-research-note`.

**Ledger (`processed_history.txt`)**  
A plain UTF-8 text file with one SHA-256 hex string per line, located at `skills/gemini-literature-processor\processed_history.txt`. The scanner loads it into a Python `set` for O(1) lookup before dispatching subprocesses. The analyzer appends to it after a successful run. `verify_and_clean.py` computes what hashes should be in the ledger (by re-reading `pdf_N_path` fields from every note and recomputing SHA-256), identifies ghost records (in ledger but note missing/PDFs gone) and orphan records (note exists but not in ledger), and can rewrite the ledger to only valid entries. `backfill_hash.py` handles the reverse direction: adds missing `combined_hash` fields into existing notes that predate the hash field.

---

## 3. Prompt Architecture

The pipeline uses **three distinct prompts** in `multifacet-spec` mode, but **only two are invoked as Gemini API calls** per paper. The `candidate_tagger` prompt exists in the skill directory but is not called from `gemini_analyze_pdf.py` — it appears to be reserved for the downstream `prefill_candidate_tags.py` or `run_tagging_pipeline.ps1` post-publish steps (outside this pipeline's scope).

**Document Profiler** (Stage A, always Flash) — classifies the PDF set and outputs a routing profile. It determines `research_domain`, `document_type`, `article_granularity`, `recommended_template`, `routing_confidence`, `is_review_like`, `is_multichapter_thesis`, and `routing_evidence`. No note content is produced here — this output is purely a routing artifact. The profile JSON is saved to `runs/<hash>/01-document-profile.json`.

**Note Generator** (Stage B, Flash or Pro depending on routing) — receives the full document profile as part of its user prompt. The system prompt (`note_generator.system.txt`) emphasizes structured JSON output only and deference to template rules. The user prompt injects the routing profile, the template ID, and concatenated template rules. The schema enforces a `frontmatter` object + `body_markdown` string + `section_diagnostics` object + `adapter_signals` object. **This stage produces the YAML frontmatter fields** (returned as a JSON object, later rendered to YAML by `build_multifacet_frontmatter()`).

**Pre-classification before note generation:** Yes — the Document Profiler always runs first. Its output determines which model processes the note and which template rules are applied. This is a hard dependency; there is no path in `multifacet-spec` mode that skips profiling.

**Template rule selection:** `document_profile["recommended_template"]` drives the file path `template_rules/<id>.txt`. The profiler can output any of the seven template IDs; `generic-research-note` is the fallback. The `_shared_rules.txt` file is always appended regardless of template.

---

## 4. Output Schema

### YAML Frontmatter Fields

Fields are written in a fixed order by `build_multifacet_frontmatter()` (line 764 of `gemini_analyze_pdf.py`):

| Field | Source | Type |
|---|---|---|
| `title_en` | Gemini Note Generator | string |
| `title_zh` | Gemini Note Generator | string |
| `authors` | Gemini Note Generator | list of strings |
| `year` | Gemini Note Generator | integer, nullable |
| `journal` | Gemini Note Generator | string |
| `doi` | Gemini Note Generator | string, nullable |
| `keywords` | Gemini Note Generator | list, 5–10 items |
| `topic` | Gemini Note Generator | list, 1–8 items |
| `research_domain` | Gemini Note Generator | enum (8 values) |
| `document_type` | Gemini Note Generator | enum (10 values) |
| `note_template` | Gemini Note Generator | enum (7 values) |
| `seed_terms` | Gemini Note Generator (sanitized) | list |
| `scope_hint` | Gemini Note Generator | enum: core / other / needs-body-evidence |
| `signal_quality` | Gemini Note Generator | enum: strong / medium / weak |
| `routing_confidence` | Gemini Note Generator | enum: high / medium / low |
| `combined_hash` | computed locally from PDF bytes | string (SHA-256 hex) |
| `pdf_0_name` | local filesystem | string |
| `pdf_0_path` | local filesystem (absolute) | string |
| `pdf_1_name` / `pdf_1_path` | if SI present | string |
| `zotero_parent_key` | Zotero SQLite lookup | string (8-char key) |
| `tags` | hard-coded empty | list |
| `candidate_tags_high` | hard-coded empty (filled by prefill) | list |
| `candidate_tags_medium` | hard-coded empty (filled by prefill) | list |
| `candidate_tags_low` | hard-coded empty (filled by prefill) | list |
| `human_reviewed` | hard-coded 0 | integer |

### Body Section Structure

From the sampled note and the `electrocatalysis-experimental.txt` template:

1. `# 文献基本信息` — recommended filename, title, journal, DOI
2. `## 英文摘要原文` — Zotero abstract injected by post-processing (if available)
3. `# 客观摘要` — dense Chinese summary with embedded English terms
4. `# 研究问题（Research Question）` — framed against prior-art gap
5. `# 体系信息（System）` — catalyst, electrode, electrolyte, reactor
6. `# 核心测试条件与定量方法` — structured condition table
7. `# 核心性能指标` — quantitative results with value + unit + condition
8. `# 逐图证据路径总结（Figure-by-Figure）` — panel-level evidence walk
9. `# 关键主张-证据路径图（claim-support map）` — per-claim causal chains
10. `# 深度机理视角提炼` — 2–4 mechanistic viewpoints
11. `# 方法亮点与审稿人陷阱扫描` — method highlights + mandatory trap-scan checklist
12. `# 主观打分` — 4-dimension scoring (0–10 each)
13. `# 核心结论总结` — final synthesis paragraph

Other templates differ in section structure: for example, `review-or-perspective` omits the Figure-by-Figure section, and `phd-dissertation` has chapter-structure sections. All templates share the `_shared_rules.txt` constraints.

---

## 5. Dedup Mechanism

**`combined_hash` definition:** SHA-256 of the sorted list of per-file SHA-256s. Sorting is order-independent — it does not matter if main.pdf is passed first or second. Specifically:
1. For each PDF in the group, compute `sha256(file_bytes)` → `file_hash`.
2. Sort all `file_hash` values lexicographically.
3. Feed the sorted file hashes into a new SHA-256 hasher (each encoded as UTF-8 text), producing `combined_hash`.

This is the "stable" hash. A "legacy" hash from earlier script versions fed the file hashes in path-sorted order (not hash-sorted), so it is path-order-dependent. The code maintains backward compatibility by accepting either hash. Both variants are stored in `get_combined_hash_variants()` returns.

**`processed_history.txt` format:** One 64-character hex string per line, UTF-8. The file is append-only during normal operation. Example first lines:
```
0030dfa325348bdbb51ea1e89fc9ae01ad73a73ce0f29f49fdbb952a5aad89a0
00da8e989989104e67d203f84bac404031a5a210fa946268d08bae1353ce8485
```
At 794 entries (as of the CLAUDE.md note), this is a ~51 KB file.

**Two-layer dedup at scan time:**
1. Ledger check: `combined_hash` (and legacy variant) checked against the `set` loaded from `processed_history.txt`.
2. Live-vault index check: a temp JSON index (`build_live_note_index()`) maps `combined_hash` and `zotero_parent_key` to note paths by scanning `$LOCALRAG_NOTES_DIR/**/*_review_note.md` frontmatters. This catches notes that exist on disk but whose hash was never written to the ledger.

**`verify_and_clean.py` reconciliation:** Reads each note's `pdf_N_path` fields, recomputes `combined_hash`, and produces three sets: valid (both ledger and note agree), ghost (ledger entry but no computable note hash), orphan (note exists but not in ledger). `--clean` rewrites the ledger to only valid hashes, saving ghost hashes to a timestamped file.

**In-frontmatter `combined_hash` usage:** The `combined_hash` field in the note frontmatter is authoritative for live-vault index resolution. If the file moves but the frontmatter is intact, the scanner can still match it via the index. `backfill_hash.py` retroactively adds this field to older notes that lack it.

---

## 6. API and Key Handling

**Authentication:** The pipeline uses Vertex AI, not the Gemini Developer API key. Authentication is via a service account JSON file:
- `GOOGLE_APPLICATION_CREDENTIALS` = `$HOME\<your-service-account>.json`
- `GOOGLE_CLOUD_PROJECT` = `<your-gcp-project>`
- `GOOGLE_CLOUD_LOCATION` = `global`
- `GEMINI_VERTEX_GCS_BUCKET` = `<your-gcs-bucket>`

There is no `GEMINI_API_KEY` used anywhere in the active pipeline. The SKILL.md explicitly states "Vertex PDF input goes through GCS objects, not `GEMINI_API_KEY`". (A `DEFAULT_PROMPT` string in `gemini_analyze_pdf.py` at line 1638 is a legacy prompt kept for reference — it is not called in `multifacet-spec` mode.)

**Multi-key rotation:** Not present. Single service account used for all calls.

**Model selection (`model_routing_policy.json`):**
- Profiler stage: always `gemini-2.5-flash` (policy key `default_profiler_model`; `pro_profiler_model` is also flash — no upgrade path for the profiler).
- Note Generator:
  - Default: `gemini-2.5-flash` (`default_note_generator_model`)
  - Upgraded to `gemini-2.5-pro` (`pro_note_generator_model`) when:
    - `primary_pdf_pages >= 30` (threshold `page_count_threshold_pro`)
    - `total_pdf_pages >= 60` (threshold `total_page_count_threshold_pro`)
    - `document_type` in `pro_document_types`: textbook, phd-dissertation, review, perspective, commentary
    - `is_review_like = true` (policy key `review_like_upgrade: true`)
    - `is_multichapter_thesis = true` (policy key `multichapter_upgrade: true`)
- CLI overrides: `--model` (all stages), `--flash-model`, `--pro-model`.

**GCS upload path:** `pdf-inputs/<combined_hash>/<idx:02d>_<safename>.pdf`. The bucket is auto-created if absent, with storage class `STANDARD` in `DEFAULT_BUCKET_LOCATION` (env var `GEMINI_VERTEX_GCS_BUCKET_LOCATION`, default `"US"`).

---

## 7. External Dependencies

**Python packages (Python 3.11 at `$LOCALRAG_MAIN_PYTHON`):**
- `google-genai` — Vertex AI Gemini client (`from google import genai`, `from google.genai import types`)
- `google-cloud-storage` — GCS upload/bucket management
- `pypdf` — PDF page count, preflight, and splitting (`PdfReader`, `PdfWriter`)
- `yaml` (PyYAML) — frontmatter parsing in scanner and verify_and_clean
- Standard library: `sqlite3`, `hashlib`, `subprocess`, `concurrent.futures`, `pathlib`, `argparse`, `re`, `json`, `shutil`, `tempfile`

**System tools:**
- No Ollama dependency in the note generation pipeline itself. Ollama (`ollama`) is only required in the post-ingest step to serve the `qwen3-embedding:4b` model for rebuilding the LocalRAG ChromaDB vector databases — that is the next layer downstream.
- PowerShell is used by the `kimi_fallback` post-publish action (`run_tagging_pipeline.ps1`) and the `restart_query` action.

---

## 8. Hard-Coded Paths and Assumptions

| Path / Assumption | Location | Notes |
|---|---|---|
| `$ZOTERO_DB_PATH` | `gemini_analyze_pdf.py` line 23, `zotero_batch_scanner.py` arg default | Zotero data dir default |
| `$ZOTERO_ATTACHMENT_BASE_DIR` | `zotero_batch_scanner.py` arg default `--base-dir` | Linked attachment base (non-ASCII path) |
| `$LOCALRAG_NOTES_DIR` | `gemini_analyze_pdf.py` line 36, `verify_and_clean.py` line 23 | Live vault root |
| `skills/gemini-literature-processor` | `gemini_analyze_pdf.py` line 24 (`CANONICAL_SKILL_ROOT`) | Resolved via env `GEMINI_LITERATURE_SKILL_ROOT` |
| `$LOCALRAG_MAIN_PYTHON` | `zotero_batch_scanner.py` line 18, `gemini_analyze_pdf.py` line 37 | Hard-coded approved Python |
| `$LOCALRAG_RAG_PYTHON` | `gemini_analyze_pdf.py` line 39 | RAG Python for post-publish |
| `$HOME\<your-service-account>.json` | `SKILL.md`, `workflow-runbook.md` | Service account credential file |
| `<your-gcp-project>` | env var `GOOGLE_CLOUD_PROJECT` | GCP project ID |
| Bucket name pattern `<project_id>-gemini-literature-temp` | `gemini_analyze_pdf.py` line 1822 | Auto-derived if `GEMINI_VERTEX_GCS_BUCKET` unset |
| Windows MAX_PATH mitigation | `gemini_analyze_pdf.py` line 216 | Chunk filenames capped to fit |
| `PYTHONUTF8=1` + `PYTHONIOENCODING=utf-8` | scanner subprocess env | Required for Chinese filenames in `<vault-with-non-ascii-paths>\` |
| GCS object prefix `pdf-inputs/<combined_hash>/` | `gemini_analyze_pdf.py` line 1868 | Archive location per paper |
| Note filename pattern `*_review_note.md` | `_iter_live_vault_note_paths()` | Discovery glob for live vault |

---

## 9. Failure Modes

**Zotero database locked:** If Zotero is open and the DB cannot be copied, `shutil.copy2()` raises an exception and `get_zotero_pdf_groups()` returns `[]`. No scan occurs. The SKILL.md requires confirming Zotero is closed before any run.

**GCS / Vertex quota (429 / RESOURCE_EXHAUSTED):** Detected in `process_group()` by checking stderr/stdout for `resource_exhausted`, `quota exceeded`, or `429`. Retry backoff is `30 * attempt` seconds (vs. `10 * attempt` for other errors), up to 3 attempts. After 3 failures the group is marked failed and skipped (hash not written to ledger, so it will be retried on the next run).

**Non-retryable PDF errors:** `PDFPreflightError` with codes `missing_pdf`, `corrupt_pdf`, or `oversize_pdf` causes the subprocess to exit with code 2 and print `NON_RETRYABLE_ERROR[<code>]` to stderr. The scanner detects this via `is_non_retryable_error_text()` and does not retry. These items remain unprocessed in the ledger.

**Malformed YAML / JSON from Gemini:** `_validate_against_schema()` raises `ValueError` if the schema is violated. `json.loads()` raises on invalid JSON. Both are unhandled at the subprocess level (the exception propagates and the subprocess exits non-zero), triggering retry logic in the scanner. There is a known YAML colon-in-value issue documented in the TODO comment at line 1 of `gemini_analyze_pdf.py` (journal titles like "Applied Catalysis B: Environmental") — the `_yaml_quote_if_needed()` function handles this for the render step, but Gemini-generated fields containing YAML-special characters may cause parse failures in downstream tools if not properly quoted.

**Encoding issues:** `PYTHONUTF8=1` and `PYTHONIOENCODING=utf-8` are forced in the subprocess environment. The `main()` function in `gemini_analyze_pdf.py` wraps `sys.stdout` and `sys.stderr` in UTF-8 wrappers. Windows paths with Chinese characters (`<vault-with-non-ascii-paths>\`) are handled as long as these env vars are set.

**GCS bucket access failure:** If the service account lacks Storage Admin permissions, `ensure_bucket()` raises and the script exits with code 1. The error message explicitly instructs granting Storage permissions.

**Oversize single PDF page:** If even a single-page chunk exceeds 50 MB, `split_pdf_for_vertex()` raises `PDFPreflightError("oversize_pdf")`. This is non-retryable.

---

## 10. Open Questions and Surprises

**Candidate Tagger prompt not invoked inline.** `candidate_tagger.system.txt` and `candidate_tagging.vertex.schema.json` exist in the skill directory, but `gemini_analyze_pdf.py` never calls them. The tagger appears to be reserved for the separate `prefill_candidate_tags.py` post-publish step. This means the `candidate_tags_*` fields in freshly generated notes are always empty (`[]`) until the prefill action runs — the "advisory" candidate tagging from Gemini is not part of the generation pass.

**Legacy `DEFAULT_PROMPT` string is dead code in multifacet-spec mode.** Lines 1638–1758 of `gemini_analyze_pdf.py` contain a large Chinese-language prompt string (`DEFAULT_PROMPT`) that was the entire prompt in the original "legacy" pipeline mode. In `multifacet-spec` mode (now the default) this string is never referenced. The legacy mode path is still executable via `--pipeline-mode legacy` but is not the recommended workflow.

**Dual hash scheme creates reconciliation complexity.** The stable hash (sort by hash value then chain) and legacy hash (sort by path then chain) are subtly different. Notes written before the hash scheme stabilized may match only on the legacy variant. `find_processed_hash_match()` accepts both, but `verify_and_clean.py` only implements the stable algorithm, potentially mis-identifying some old notes as orphans if their PDF paths have changed.

**GCS objects are never deleted.** The `cleanup_gcs_archive.py` script is listed in SKILL.md primary scripts, but its content was not examined in this investigation. The upload path `pdf-inputs/<combined_hash>/` accumulates indefinitely without cleanup in the main pipeline. The bucket functions as a permanent archive, not a temp staging area — despite being named `...-gemini-literature-temp`.

**`zotero_parent_key` lookup uses filename matching, not path.** `get_parent_key()` queries `WHERE ia.path LIKE '%<filename>%'` — a substring match on just the basename. If two Zotero items have attachments with the same filename, this returns whichever row appears first (LIMIT 1), and the `zotero_parent_key` field in the note may be incorrect. This is a latent correctness bug.

**`prefs.js` auto-detection is racy.** The scanner tries to auto-detect `baseAttachmentPath` from `%APPDATA%\Zotero\Zotero\Profiles\*\prefs.js`, but this read happens while the Zotero process may have the file open (Zotero is supposed to be closed, but the detection runs before any confirmation). If the pref file is being written, the regex may get a partial read and silently fall back to `manual_base_dir`.

**No retry on GCS upload timeout.** The GCS upload timeout is configurable via `GEMINI_GCS_UPLOAD_TIMEOUT_SECONDS` (default 900 s), but if it triggers, the exception bubbles up through `upload_pdfs_to_gcs()` and the subprocess exits non-zero. The scanner treats this as a generic error and retries after 10 s — but the PDF remains partially uploaded to GCS, and a re-upload of the same object will overwrite it cleanly (GCS is idempotent for object writes), so this is recoverable.

**`--pipeline-mode legacy` produces a different output format.** Legacy mode uses the `DEFAULT_PROMPT` and writes a YAML code-fenced block inside Markdown (the old format). The scanner hardcodes `--pipeline-mode multifacet-spec` as the recommended value in all runbook recipes. Mixing modes in the same vault would produce notes with incompatible frontmatter structures.
