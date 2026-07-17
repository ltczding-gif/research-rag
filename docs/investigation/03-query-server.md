# 03 — Query Server API Investigation

**File:** `$LOCALRAG_HOME\query_server.py` (~1128 lines)
**Investigated:** 2026-05-08

---

## 1. Server Architecture

**Framework:** Flask (imported at line 7: `from flask import Flask, request, jsonify`)

**Port:** `18810` — hardcoded at line 1125: `PORT = 18810`, bound to `host='127.0.0.1'`

**Port discrepancy:** All three consumer SKILL.md files (`search-literature`, `search-notes`, `search-papers`) reference `http://127.0.0.1:18800`, not 18810. This is a live mismatch — either the skills are pointing at the wrong port or the server was moved to 18810 without updating the skills. The `query_server.py` `__main__` block and the `SKILL.md` launch snippet in `search-literature\SKILL.md` (line 34) both show different ports.

**Threading:** Flask's built-in single-threaded Werkzeug WSGI server (`debug=False`, no `threaded=True` argument). Concurrent requests will queue. No gunicorn or uWSGI wrapper.

**Startup:** Launched via `Start-Process` from PowerShell. From `search-literature\SKILL.md` lines 32–35:
```powershell
$PYTHON_RAG = "$LOCALRAG_RAG_PYTHON"
Start-Process $PYTHON_RAG -ArgumentList "$LOCALRAG_HOME\query_server.py" -WindowStyle Hidden
```
The skill also ensures Ollama is running before starting the server. There is no process supervisor, systemd equivalent, or Windows service wrapper — if the Python process dies, skills will start getting 503s silently.

**ChromaDB initialization at startup (lines 39–80):** Controlled by the env var `LOCALRAG_SKIP_CHROMA_INIT`. When `0` (default), startup connects to the persistent ChromaDB at `$LOCALRAG_HOME\chroma`, binds the `OllamaEmbeddingFunction` to the `papers` collection, then opens the `notes` collection without binding an embedding function (notes collection uses manually supplied embeddings at query time). Either collection failing logs a warning but does not crash startup — the server comes up with `chroma_ready=False` or `notes_ready=False` and returns 503 on affected endpoints.

---

## 2. Endpoint Inventory

### `GET /health`
**Lines 528–571**

- **Request:** No body.
- **Response:** JSON with `status`, `papers.ready/path/collection/chunks`, `notes.ready/count`, `ollama` (live-probed with a test embedding call, 5s timeout).
- **HTTP code:** 200 if both `papers.ready` and `notes.ready` are true; 503 otherwise.
- **Behavior:** Useful liveness probe. Ollama is actively pinged on every health call — not cached.

---

### `POST /search_notes`
**Lines 574–595, implementation at lines 472–525**

- **Request body:**
  ```json
  { "query": "string (required)", "n": 5, "dedupe": true, "zotero_parent_key": "optional string" }
  ```
- **Response:**
  ```json
  {
    "results": [
      {
        "id": "chroma doc id",
        "content": "first 3000 chars of note",
        "metadata": {
          "source_file": "...", "zotero_parent_key": "...",
          "title_en": "...", "title_zh": "...",
          "year": "...", "journal": "...", "authors": "...", "doi": "...",
          "score": 0.9234,   // 1 - cosine_distance
          "note_rank": 1     // 1-based rank in result set
        }
      }
    ]
  }
  ```
- **Behavior:**
  1. Calls `get_embedding(query)` — a manual Ollama HTTP POST (60s timeout, truncates query at 12 000 chars).
  2. Passes the embedding vector directly to `notes_col.query(query_embeddings=[...], n_results=n, where=...)`.
  3. The `notes` collection was built with manually computed embeddings (no bound `ef`), so the manual embedding call is required.
  4. If `zotero_parent_key` is supplied, adds a ChromaDB `where={"zotero_parent_key": key}` filter.
  5. Parses YAML frontmatter from each returned document to supplement any metadata fields missing from ChromaDB metadata.
  6. Content is truncated at 3000 characters per result.
- **`dedupe` parameter:** Accepted but **ignored** in the current implementation. The comment at line 484 explains: "dedupe 对整篇入库无意义（每篇只有一条记录），直接取 top-n". Because notes are stored one-per-document (not chunked), each ChromaDB result is already a distinct paper.
- **Error:** 500 with `{"error": "...", "traceback": "..."}` if Ollama is down or ChromaDB fails; 503 if `notes_col` is None at startup.

---

### `POST /get_note`
**Lines 598–655**

- **Request body:**
  ```json
  { "source": "$LOCALRAG_NOTES_DIR/xxx.md", "zotero_parent_key": "ABC12345", "summary_only": false }
  ```
  Requires either `source` or `zotero_parent_key`. `summary_only` is optional (default false).
- **Response:**
  ```json
  {
    "notes": [
      {
        "source": "filename from metadata",
        "content": "full note or first 500 chars + '...'",
        "summary_only": false,
        "metadata": { "title_en", "title_zh", "year", "journal", "zotero_parent_key", "doi" }
      }
    ]
  }
  ```
- **Behavior:**
  1. Uses `notes_col.get(where=..., limit=5)` — **not** a vector query; a metadata filter fetch.
  2. If `source` is provided, filters by `source_file == basename(source)` (strips directory prefix).
  3. If `zotero_parent_key` is provided, filters by that key directly.
  4. `summary_only=True` returns `doc[:500] + "\n..."`.
  5. Returns up to 5 matching notes (there should only be one per key in practice).
- **Error:** 404 if no results; 503 if notes not initialized.

---

### `POST /search_papers`
**Lines 658–759**

- **Request body:**
  ```json
  {
    "query": "string (required)",
    "n": 3,
    "zotero_parent_key": "ABC12345",
    "paper_group": 1,
    "pdf_filename": "foo.pdf",
    "second_query": "English term version",
    "include_context": false
  }
  ```
- **Response:**
  ```json
  {
    "results": [
      {
        "content": "matched chunk text (~800 chars)",
        "context": "[prev_chunk] [MATCH]matched[/MATCH] [next_chunk]",
        "metadata": { "pdf_filename", "zotero_parent_key", "is_main", "is_si", "chunk_index", "paper_group", "file_index", ... },
        "distance": 0.234
      }
    ],
    "query": "original query",
    "effective_query": "query actually used for embedding",
    "filters": { ... }
  }
  ```
- **Behavior:**
  1. The `papers` collection has a bound `OllamaEmbeddingFunction`, so ChromaDB handles embedding internally via `query_texts=[effective_query]`.
  2. **`second_query` semantics (line 679–681):** If `second_query` is present, it becomes `effective_query` instead of `query`. The SKILL.md describes this as "WF4: the English translation of the note conclusion". The original `query` is still returned in the response for logging, but the vector search uses only `second_query`. This lets skills search the English PDF corpus with an English reformulation derived from the Chinese-language note hits.
  3. **Filter priority (lines 687–692):** `zotero_parent_key` > `paper_group` > `pdf_filename`. Only one filter is applied (first non-null wins). `zotero_parent_key` is the recommended modern filter; `paper_group` and `pdf_filename` are legacy.
  4. **`include_context` (lines 701–747):** When true, fetches the previous and next chunks by constructing deterministic chunk IDs (`group_{pg}_file_{file_index}_chunk_{chunk_idx±1}`), retrieves them via `pdf_col.get(ids=[...])`, and concatenates: `prev_text + "[MATCH]content[/MATCH]" + next_text`. On failure, falls back to returning content alone. This adds ~2 additional ChromaDB `.get()` calls per result.
- **Error:** 503 if `chroma_ready=False`; 500 with error string on ChromaDB exception.

---

### `POST /write_query_log`
**Lines 762–885**

- **Required fields:** `workflow_id`, `workflow_name`, `status`, `query`, `anchor_query`, `final_response_snapshot`, `idempotency_key`, `planned_angles` (non-empty list), `executed_angles` (non-empty list), `search_runs` (non-empty list).
- **Request body (partial):**
  ```json
  {
    "workflow_id": "WF4",
    "workflow_name": "笔记+原文交叉验证",
    "status": "success",
    "query": "用户原始问题",
    "anchor_query": "锚定查询",
    "final_response_snapshot": "最终回答",
    "idempotency_key": "uuid-style-string",
    "planned_angles": ["angle1", "angle2"],
    "executed_angles": ["angle1"],
    "search_runs": [ { "role": "...", "purpose": "...", "endpoint": "...", "query": "...", "filters": {}, "hits": 3 } ],
    "notes": [...],
    "papers": [...],
    "created_at": "2026-05-08T10:00:00+08:00"
  }
  ```
- **Response:**
  ```json
  { "success": true, "created": true, "deduplicated": false, "log_id": "ql-20260508-100000-AB12", "log_path": "$LOCALRAG_NOTES_DIR\\_query_logs\\2026-05\\...", "month": "2026-05" }
  ```
- **Idempotency:** Checks `_registry.json` (at `$LOCALRAG_NOTES_DIR\_query_logs\_registry.json`) before writing. If `idempotency_key` already exists **and** the referenced file still exists on disk, returns `{"created": false, "deduplicated": true}` with the existing `log_id` and `log_path` — no write occurs.
- **Behavior:** Full Markdown document is rendered via `render_query_log_markdown()` and written to `$LOCALRAG_NOTES_DIR\_query_logs\{YYYY-MM}\{timestamp}_{wf}_{slug}_{shortid}.md`. Registry is updated atomically (read → modify → write, no file lock).
- **Allowed `status` values:** `"success"`, `"no_hits"`, `"partial"`, `"error"` (line 35).

---

### `POST /append_query_log_action`
**Lines 888–933**

- **Request body:**
  ```json
  {
    "log_path": "$LOCALRAG_NOTES_DIR\\_query_logs\\2026-05\\....md",
    "log_id": "ql-20260508-100000-AB12",
    "action": "write_to_feishu",
    "result": "success",
    "timestamp": "2026-05-08T10:05:00+08:00",
    "details": { "document_id": "<doc-id>", "blocks_added": 5 }
  }
  ```
- **Required:** `log_path`, `log_id`, `action`, `result`.
- **Behavior:**
  1. Reads existing log file, verifies `log_id` matches frontmatter (prevents misrouted appends).
  2. Checks that `log_path` is within `QUERY_LOG_ROOT` (path traversal guard, line 905).
  3. Ensures `## Follow-up Actions` section header exists (adds it if absent).
  4. Appends a `### {timestamp}` sub-block with `Action`, `Result`, and any `details` key-value pairs.
  5. File is rewritten in-place (read → append → write).

---

### `POST /write_to_feishu`
**Lines 1038–1112**

- **Request body:**
  ```json
  {
    "document_id": "<doc-id>",
    "workflow": "WF4",
    "query": "用户查询",
    "notes": [...],
    "papers": [...]
  }
  ```
- **Response:** `{"success": true, "week": "2026-W18", "document_id": "...", "blocks_added": 5}`
- **Behavior:** See Section 6.

---

## 3. Embedding + Retrieval Flow

### Notes path (`/search_notes`)
```
user query
  → get_embedding(query)            # urllib POST to Ollama :11434, model qwen3-embedding:4b
  → notes_col.query(                # ChromaDB ANN search
      query_embeddings=[vec],
      n_results=n,
      where={"zotero_parent_key": key}  # optional
    )
  → parse_frontmatter(doc)          # supplement missing metadata from YAML
  → truncate content to 3000 chars
  → return ranked results with score = 1 - cosine_distance
```

### Papers path (`/search_papers`)
```
user query (+ optional second_query)
  → effective_query = second_query or query
  → pdf_col.query(                  # ChromaDB uses bound OllamaEmbeddingFunction internally
      query_texts=[effective_query],
      n_results=n,
      where={"zotero_parent_key": key}  # or paper_group, or pdf_filename
    )
  → [if include_context]:
      pdf_col.get(ids=[prev_chunk_id, next_chunk_id])
      → concatenate: prev + [MATCH]match[/MATCH] + next
  → return results with distance (raw cosine distance, not converted to score)
```

**Key asymmetry:** Notes uses `query_embeddings` (manual embed call); Papers uses `query_texts` (bound ef). This is because `notes_col` was built without binding an ef at collection creation time (line 67: `client.get_collection(name=NOTES_COLLECTION_NAME)` — no `embedding_function=ef`).

**`dedupe=True` semantics:** The parameter exists in the `/search_notes` request body and is passed to `search_notes_chroma()`, but the function explicitly ignores it (line 484–485). The docstring acknowledges it: notes are stored one-per-paper, so deduplication is structurally unnecessary. The parameter is retained for API compatibility with the skills (which pass it by convention).

**`second_query` semantics:** In WF4 (note+paper cross-validation workflow), the skill first queries `/search_notes` for Chinese-language notes, extracts key English technical terms from the note conclusions, and re-queries `/search_papers` with those English terms as `second_query`. The original Chinese `query` is still echoed back in the response. This two-pass design ensures the English PDF corpus is searched with English terminology even when the user's original query was in Chinese.

**`include_context`:** Fetches the immediately preceding and following chunks by constructing IDs from the pattern `group_{paper_group}_file_{file_index}_chunk_{chunk_index±1}` (lines 724–738). This ID scheme must match what `build_pdf_db.py` used at ingest time. If the neighbor chunk doesn't exist (first/last chunk of a file), `pdf_col.get()` silently returns no document for that ID, and `neighbor_docs.get(prev_id, '')` returns empty string. The marked chunk is always present in the output.

---

## 4. Filter Semantics

`zotero_parent_key` is stored as a ChromaDB metadata field on every chunk (both `papers` and `notes` collections). Filtering with `where={"zotero_parent_key": key}` passes directly to ChromaDB's metadata filter, which applies as an exact-match pre-filter before ANN scoring.

**Coverage of main PDF + SI:** Both the main paper PDF and the supplementary information (SI) PDF for a given Zotero item are ingested under the same `zotero_parent_key`. Chunks from the SI have `is_si=True` in their metadata; chunks from the main PDF have `is_main=True`. Because the filter operates on `zotero_parent_key` alone, a single filter call returns chunks from both documents. The `/search_papers` response surfaces `is_main` and `is_si` per chunk (lines 373–374 of the log renderer, and lines 1024–1025 in `build_search_result_blocks()`).

**Legacy filters:** `paper_group` (integer, 1–6) and `pdf_filename` (string) are earlier metadata fields from before the Zotero key system was introduced. They still work for older ingested chunks but are not set on newer ones. The filter priority logic at lines 687–692 ensures `zotero_parent_key` wins if all three are supplied.

---

## 5. Query Log Format

**Root directory:** `$LOCALRAG_NOTES_DIR\_query_logs\` (constant `QUERY_LOG_ROOT`, line 33)

**Registry file:** `$LOCALRAG_NOTES_DIR\_query_logs\_registry.json` — flat JSON dict mapping `idempotency_key → {log_id, log_path, month, created_at}`. Used for idempotency checking on `/write_query_log`.

**Monthly subdirectory naming:** `{YYYY-MM}\` (e.g., `2026-05\`)

**Filename convention:**
```
{YYYYMMDD-HHMMSS}_{workflow_id}_{query-slug}_{SHORT_ID}.md
```
Example: `20260508-100000_WF4_有哪些论文研究了催化剂_AB12.md`

The `query-slug` is generated by `slugify_query_title()` (lines 120–130): strips filesystem-illegal characters, collapses spaces to hyphens, truncates to 32 chars. The `short_id` is either caller-supplied or a 4-char uppercase hex from `uuid4()`.

**`log_id` format:** `ql-{YYYYMMDD-HHMMSS}-{SHORT_ID}` (e.g., `ql-20260508-100000-AB12`), line 135.

**Markdown structure rendered by `render_query_log_markdown()` (lines 415–450):**
```
---
(YAML frontmatter, ~30 fields — see render_frontmatter())
---

# Query Record

## User Query
## Workflow Decision
## Search Plan
## Search Runs
  ### Run 1
  ### Run 2
  ...
## Result Summary
## Notes Hits
  ### N1, N2, ...
## Paper Hits
  ### P1, P2, ...
## Final Response Snapshot
## Follow-up Actions
```

**Frontmatter key fields (lines 257–290):**
- `schema_version: 1`
- `log_id`, `idempotency_key`, `created_at`, `month`
- `workflow_id`, `workflow_name`, `status`
- `query`, `query_title`, `session_summary_title`, `query_language`
- `anchor_query`, `anchor_query_source`
- `saved_by` (defaults to `"search-literature"`)
- `search_runs` (count of runs), `search_run_details` (full array)
- `planned_angles`, `executed_angles`, `expansion_reason`, `stop_reason`
- `notes_hits`, `papers_hits`, `zotero_parent_keys`, `source_note_files`
- `effective_queries`, `second_queries`
- `feishu_written`, `feishu_document_id`
- `log_path`

**Idempotency:** The `idempotency_key` (caller-supplied, typically a UUID) is checked against the registry before any write. If the key exists and the file is still on disk, the endpoint returns immediately with `"deduplicated": true` and the existing path — no overwrite.

**Follow-up append mechanism:** `/append_query_log_action` appends `### {timestamp}` blocks under `## Follow-up Actions`. This section is added dynamically if absent (`ensure_followup_header()`, lines 457–460). Each block includes `Action`, `Result`, and arbitrary `details` key-value pairs rendered as bullet points. The `log_id` in the request is verified against the file's frontmatter before writing.

---

## 6. Feishu Writeback

**Auth method:** Feishu internal app OAuth. `get_feishu_token()` (lines 938–953) POSTs `{app_id, app_secret}` to `https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal` and returns a short-lived `tenant_access_token`. This token is fetched fresh on every `/write_to_feishu` call — not cached.

**`FEISHU_APP_ID`:** Hardcoded at line 28: `"<REDACTED-APP-ID>"` — **this must be redacted before open-sourcing**.

**`FEISHU_APP_SECRET`:** Read from environment variable `FEISHU_APP_SECRET` (line 29: `os.environ.get("FEISHU_APP_SECRET", "")`). If the env var is absent, `get_feishu_token()` returns `None` and the endpoint returns 500 with a descriptive error.

**What gets written:** `build_search_result_blocks()` (lines 974–1036) constructs a list of Feishu Block API objects:
1. A `heading2` block: `{timestamp}  [{workflow}] {query}`
2. For each note hit: a `text` block with source filename, similarity score, and 200-char content preview.
3. For each paper hit: a `text` block with PDF filename, type (主文/SI), and 500-char content.
4. A horizontal rule (`block_type: 22`).

The blocks are appended at the end of the document's root block (after all existing children).

**"本周文档未注册" flow (lines 955–972):**
- `get_current_week_doc()` computes `week_key = datetime.now().strftime("%Y-W%W")` (Python `%W` = Monday-based ISO week number) and looks it up in `$LOCALRAG_HOME\feishu_docs.json`.
- If not registered and no `document_id` in the request body, returns 400 with `"error": "本周（{week_key}）尚未注册文档"` and a hint asking for the doc URL.
- If `document_id` is supplied in the request and differs from the registered one, `register_week_doc()` updates the registry.

**Registry file:** `$LOCALRAG_HOME\feishu_docs.json` — flat JSON dict keyed by `"YYYY-WWW"` strings mapping to Feishu `document_id` strings. Current content (as of investigation): `{"2026-W11": "<doc-id>"}` — only one week registered, week 11 of 2026.

**No canonical Feishu helper module is imported.** All Feishu logic is self-contained within `query_server.py` using `urllib.request`. The 20+ `upload_*.py`, `create_doc*.py`, `check_*.py` etc. files in `$LOCALRAG_HOME\` are **standalone scratch experiments** — none are imported by `query_server.py`. They were created during the Feishu integration development process and are candidates for deletion.

---

## 7. Hard-coded Paths, Secrets, and URLs

| Constant | Value | Location | Open-source risk |
|---|---|---|---|
| `CHROMA_PATH` | `$LOCALRAG_HOME\chroma` | line 21 | Path disclosure |
| `COLLECTION_NAME` | `"papers"` | line 22 | None |
| `NOTES_COLLECTION_NAME` | `"notes"` | line 25 | None |
| `FEISHU_APP_ID` | `"<REDACTED-APP-ID>"` | line 28 | **Must redact** |
| `FEISHU_APP_SECRET` | env var (safe) | line 29 | None |
| `FEISHU_DOCS_REGISTRY` | `$LOCALRAG_HOME\feishu_docs.json` | line 30 | Path disclosure |
| `QUERY_LOG_ROOT` | `$LOCALRAG_NOTES_DIR\_query_logs` | line 33 | Path disclosure |
| `QUERY_LOG_SCHEMA_VERSION` | `1` | line 34 | None |
| Ollama URL | `http://localhost:11434/api/embeddings` | lines 50, 89, 562 | None (localhost) |
| Ollama model | `"qwen3-embedding:4b"` | lines 50, 89, 562 | None |
| Feishu API | `https://open.feishu.cn/open-apis/...` | lines 942, 1073, 1088 | None (public API) |
| `feishu_docs.json` doc ID | `"<doc-id>"` | registry file | Minor (doc ID) |
| Server bind | `127.0.0.1:18810` | line 1127 | None |

**Open-sourcing blockers:** `FEISHU_APP_ID` (line 28) must be moved to an env var. All absolute Windows paths should become configurable (env vars or a config file).

---

## 8. `query_server_v2.py` Status

**Status: Deprecated early experiment. Not in use.**

`query_server_v2.py` (228 lines) was a prior iteration with a fundamentally different architecture for `/search_notes`:
- Used SQLite directly (`~\.openclaw\memory\main.sqlite`) instead of ChromaDB.
- `search_memory_sqlite()` does a `LIKE '%query%'` text search — no vector embeddings for notes.
- `/search_notes` takes only `{query, n}` — no `dedupe`, no `zotero_parent_key`, no frontmatter parsing.
- `/search_papers` is simplified: only `paper_group` and `pdf_filename` filters; no `zotero_parent_key`, no `second_query`, no `include_context`.
- No `/get_note`, `/write_query_log`, `/append_query_log_action`, `/write_to_feishu` endpoints.
- No Feishu integration, no query log system.

It represents an early version before the ChromaDB notes collection and the Zotero-key–based system were introduced. It can be archived or deleted.

---

## 9. Failure Modes

| Scenario | Behavior |
|---|---|
| **Ollama down at startup** | `OllamaEmbeddingFunction` init may still succeed (it may not probe until first query); `papers` collection opens. First `/search_papers` call triggers ChromaDB to embed the query — will fail with a connection error, returning 500. `/search_notes` fails at `get_embedding()` call, returning 500 with traceback. |
| **Ollama down at query time** | `/search_notes`: 500, `{"error": "<URLError>", "traceback": "..."}`. `/search_papers`: 500, ChromaDB error propagated. `/health`: `"ollama": "error: <URLError>"`, 503. |
| **ChromaDB `papers` collection missing** | `chroma_ready=False` at startup. `/search_papers` returns 503 immediately. `/health` returns 503. |
| **ChromaDB `notes` collection missing** | `notes_ready=False`. `/search_notes` and `/get_note` return 500 with `"Notes collection not initialized"`. |
| **`FEISHU_APP_SECRET` env var absent** | `get_feishu_token()` returns `None`. `/write_to_feishu` returns 500 with `"飞书 token 获取失败，检查 FEISHU_APP_SECRET 环境变量"`. |
| **`feishu_docs.json` week key missing** | `/write_to_feishu` without `document_id` returns 400 with registration hint. |
| **Malformed JSON request body** | Flask's `request.json` returns `{}` (not an error), so missing required fields are caught by the explicit `missing` check (line 779) and return 400. |
| **`zotero_parent_key` not in ChromaDB** | ChromaDB `where=` filter returns zero results. Response is `{"results": []}` — not an error. |
| **Log file deleted after registry write** | `is_path_within_root` check on `/append_query_log_action` returns 404. On `/write_query_log`, if the file is gone, idempotency check fails (file doesn't exist), so a new log is written. |
| **Concurrent writes to query log** | No file lock. Concurrent `/append_query_log_action` or `/write_query_log` calls can race (read-modify-write). Low risk given single-threaded Flask and typical usage patterns. |
| **Port already in use (18810)** | Server fails to bind and the Python process exits immediately. Skills will see connection refused. |

---

## 10. Open Questions and Cleanup Candidates

### Port mismatch
All three consumer skills reference port `18800`; `query_server.py` binds to `18810`. One of these is wrong. This needs to be reconciled — either update the SKILL.md files or the server's `PORT` constant.

### Scratch experiment files (candidates for cleanup)
The following 30+ files in `$LOCALRAG_HOME\` appear to be one-off development experiments for the Feishu integration and ChromaDB debugging. None are imported by `query_server.py`. All are candidates for deletion or archiving:

**Feishu upload experiments (25 files):**
`upload_to_feishu.py`, `upload_feishu_final.py`, `upload_feishu_final_v2.py`, `upload_final_v3.py`, `upload_v4.py`, `upload_correct_multipart.py`, `upload_explorer.py`, `upload_explorer_v2.py`, `upload_direct.py`, `upload_httpclient.py`, `upload_mime.py`, `upload_multipart.py`, `simple_upload.py`, `upload_folder.py`, `try_all_methods.py`, `create_doc.py`, `create_doc2.py`, `create_docs.py`, `create_docs_v2.py`, `create_docs_v3.py`, `batch_create.py`, `write_doc_content.py`, `fix_doc_content.py`, `batch_trash.py`, `try_delete_methods.py`, `delete_duplicates.py`

**Feishu inspection scripts (5 files):**
`check_feishu.py`, `check_doc.py`, `check_status.py`, `verify_upload.py`, `analyze_folder.py`

**ChromaDB debug scripts (5 files):**
`check_db.py`, `check_db2.py`, `check_db3.py`, `check_db4.py`, `test_build.py`, `test_like.py`, `create_feishu_file.py`, `test_feishu_upload.py`

**Deprecated server:**
`query_server_v2.py` — superseded by `query_server.py`.

### Flask threading
Flask's development server is single-threaded. If any upstream call (Ollama, Feishu API) blocks, the entire server queues. Consider adding `threaded=True` to `app.run()` or migrating to gunicorn for production use.

### Feishu week key calculation
`datetime.now().strftime("%Y-W%W")` uses Python's `%W` (Monday-based week, 00–53). Week 1 starts on the first Monday of the year. This may produce `W00` for days before the first Monday. Consider using `isocalendar()` for ISO 8601-compliant week keys to avoid off-by-one at year boundaries.

### Missing `include_context` flag in legacy chunk ID scheme
The `include_context` neighbor lookup constructs chunk IDs assuming the format `group_{paper_group}_file_{file_index}_chunk_{chunk_index}`. Textbook chunks ingested via `ingest_textbook.py` may use a different ID scheme. If so, context retrieval silently falls back to content-only.

### `notes_col` embedding function gap
The `notes` collection has no bound embedding function. If `build_notes_db.py` is ever re-run with a different embedding model, the manual `get_embedding()` call in `search_notes_chroma()` must also be updated manually — there's no single source of truth for which model the notes collection uses.
