# research-rag — staging status

This file tracks what's been done in the staging repo and what's left before
the first public release. It's an executive summary of `docs/PACKAGING-PLAN.md`.

Generated: 2026-05-08.

## Source provenance

This repo is a staging copy. The originals were at:

| What | Original location |
|---|---|
| `service/*.py` | `$LOCALRAG_HOME\` |
| `scanner/*.py`, `prompts/`, `schemas/`, `template_rules/`, `references/` | `skills/gemini-literature-processor\` |
| `skills/search-literature/`, `skills/search-notes/`, `skills/search-papers/`, `skills/literature-tagging-pipeline/`, `skills/{rag-engineer,vector-database-engineer,embedding-strategies}/` | `$HOME\.claude\skills\<name>\` |
| `skills/gemini-literature-processor/` (canonical) | `skills/gemini-literature-processor\` |
| `skills/literature-tagging-pipeline/scripts/` | `skills/literature-tagging-pipeline/scripts/` |
| `docs/` | `$LOCALRAG_NOTES_DIR\.system-export\` |

The originals were **not modified**. All edits live in this staging directory.

## What was done

### ✅ Phase 1 — repo skeleton + file copy
- Created `$REPO_ROOT/` tree: `service/`, `scanner/`, `skills/`, `docs/`.
- Copied source files from canonical locations only. Excluded:
  - 30+ Feishu upload experiment scripts (`upload_*.py`, `create_doc*.py`, etc.)
  - 4 `check_db*.py` files (target a different SQLite system, not ChromaDB)
  - `query_server_v2.py` (deprecated SQLite-based variant)
  - `wave8_gold_ledger.txt` (unrelated annotation project)
  - All `.bak`, `.log`, `__pycache__`, scratch test files
  - Per-user state files (`processed_*.txt`)

### ✅ Phase 2 — path templating (P0)
- Created `service/config.py` and `scanner/config.py` as central env-var-aware config layers.
- Refactored 6 source files to read all paths/URLs/credentials from env vars:
  - `service/query_server.py` — CHROMA_PATH, QUERY_LOG_ROOT, OLLAMA_URL, EMBED_MODEL, HOST, PORT
  - `service/build_pdf_db.py` — same + ZOTERO_DB, NOTES_DIR, PDF_LEDGER, CHUNK_SIZE, CHUNK_STEP, MIN_CHUNK_LEN
  - `service/build_notes_db.py` — same + NOTES_LEDGER, NOTE_SUFFIX, MAX_EMBED_CHARS
  - `service/ingest_textbook.py` — same + TEXTBOOK_LEDGER, TEXTBOOK_BATCH_SIZE
  - `scanner/zotero_batch_scanner.py` — DEFAULT_VAULT_ROOT, APPROVED_MAIN_PYTHON, ZOTERO_DATA_DIR, argparse defaults
  - `scanner/gemini_analyze_pdf.py` — ZOTERO_DB_PATH, CANONICAL_SKILL_ROOT, DEFAULT_VAULT_ROOT, APPROVED_MAIN_PYTHON, APPROVED_RAG_PYTHON, BUILD_*_DB_PATH, QUERY_SERVER_PATH, etc.

### ✅ Phase 3 — drift fixes (P0)
- Port unified at **18810** in all 4 SKILL.md files. The leaf `search-notes` and `search-papers` SKILLs were stuck at 18800.
- `gemini-literature-processor/SKILL.md` rewritten to clearly document Vertex AI service-account auth (the legacy `GEMINI_API_KEY` rotation path is no longer documented since the code never used it).
- `dedupe` parameter on `/search_notes` documented as a no-op for backward compatibility (notes are stored 1-per-doc, so nothing to dedupe).
- All hardcoded `$HOME\…`, `<your-data-drive>\…`, `<vault-with-non-ascii-paths>\…`, `$LOCALRAG_NOTES_DIR\…` paths in SKILL.md files replaced with env-var references (`$LOCALRAG_NOTES_DIR`, etc.) or repo-relative paths (`service/build_pdf_db.py`).
- Cross-platform startup snippets added (Unix bash + Windows PowerShell).

### ✅ Phase 4 — bootstrap files
- `.env.example` — full env-var contract (with required/optional markings)
- `.gitignore` — secrets, venvs, ChromaDB data, ledgers, logs, user state
- `requirements-rag.txt` (chromadb 1.5.5 pinned, flask, pdfplumber, pyyaml)
- `requirements-scanner.txt` (vertex + gemini-api default deps; anthropic / openai opt-in)
- `setup.sh` (Unix bootstrap: 2 venvs + Ollama health check)
- `setup.ps1` (Windows bootstrap: same)

### ✅ Phase 8 — OpenAI-compatible backend
Added `OpenAIBackend` (`scanner/backends/openai_api.py`) so the scanner works
with any provider exposing the OpenAI Chat Completions protocol:

- OpenAI Inc. (gpt-4o, gpt-4o-mini, ...)
- DeepSeek (`OPENAI_BASE_URL=https://api.deepseek.com/v1`)
- Mistral, OpenRouter, Together, Groq, Qwen, vLLM, Ollama OpenAI compat,
  LM Studio, etc. — same pattern.

Key implementation choices:
- **PDF transport**: extracts page text locally with `pdfplumber` and
  bundles into the user message. Universally portable (no provider needs
  to support PDF input), but **figures and tables are lost** — the model
  only sees what pdfplumber can extract. Vertex / Gemini API / Anthropic
  remain the higher-fidelity options for figure-aware analysis.
- **Structured output**: enforced via OpenAI tool calling. JSON schema
  becomes the tool's `parameters`, and `tool_choice` forces the call.
  Falls back to parsing JSON from `message.content` if the provider
  ignores `tool_choice` (a few compatible providers do).
- **Model translation**: same flash/pro tier abstraction. Defaults to
  `gpt-4o-mini` and `gpt-4o`; overridable via `OPENAI_FLASH_MODEL` and
  `OPENAI_PRO_MODEL` (e.g. `deepseek-chat` / `deepseek-reasoner`).
- **Truncation**: `max_chars_per_pdf=200_000` to stay under most
  providers' context limits; truncations are flagged in the prompt.

CLI / env wiring:
- `--backend openai` choice in `gemini_analyze_pdf.py` and
  `zotero_batch_scanner.py`.
- `OPENAI_API_KEY`, `OPENAI_BASE_URL`, `OPENAI_ORG_ID`, `OPENAI_FLASH_MODEL`,
  `OPENAI_PRO_MODEL` added to `scanner/config.py` and `.env.example`.

Deps:
- Renamed `requirements-gemini.txt` → `requirements-scanner.txt` and
  reorganized by backend. `openai` and `anthropic` are commented opt-in.
- `setup.sh` and `setup.ps1` updated.

Docs:
- SKILL.md: added openai row to backend table, env block, switch examples,
  caveat about figure loss in text-only PDF transport.
- README, ARCHITECTURE: backend list updated.
- This Phase 8 entry.

### ✅ Phase 7 — Pluggable processor backends
Universalized the note generator so it isn't hard-bound to Vertex AI Gemini.

- New package `scanner/backends/` with `ProcessorBackend` ABC + 4 implementations:
  - `vertex.py` — original GCS-upload path (production default).
  - `gemini_api.py` — direct Google AI Studio API key, inline `Part.from_bytes`.
  - `anthropic_api.py` — Anthropic Claude with base64 `document` content blocks; structured output enforced via tool-use (schema = tool's `input_schema`, `tool_choice` forced).
  - `subagent.py` — no API call. Writes a self-describing manifest under `runs/<combined_hash>/manifest-<stage>.json` and raises `SubagentManifestPending`. The user's Claude Code session then runs the model calls via the Task tool; a `--resume` finalize path is on the roadmap.
- `scanner/gemini_analyze_pdf.py`:
  - `run_document_profiler` and `run_note_generator` now take a `backend` instead of `(client, pdf_parts)`.
  - `run_multifacet_spec_pipeline` rewired the same way.
  - New `make_backend_from_args(args)` factory wires CLI/env to the right backend.
  - Main flow catches `SubagentManifestPending` and exits cleanly.
  - Legacy `--pipeline-mode legacy` still works but is now `--backend vertex` only (errors out otherwise).
- `scanner/zotero_batch_scanner.py`: forwards `--backend` to gemini_analyze_pdf.py.
- `scanner/config.py`: added `LOCALRAG_PROCESSOR_BACKEND`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`, `ANTHROPIC_FLASH_MODEL`, `ANTHROPIC_PRO_MODEL`.
- `.env.example`: backend-selection block + per-backend credential blocks (only fill in the one you use).
- `skills/gemini-literature-processor/SKILL.md`: backend selection table, per-backend env var blocks, sub-agent dispatch instructions.
- `docs/ARCHITECTURE.md`: new "后端可插拔" section + flash/pro tier abstraction note.

What stayed identical across backends: prompts (`prompts/*.system.txt`), JSON schemas (`schemas/*.vertex.schema.json`), template rules (`template_rules/*.txt`), routing policy, frontmatter render, ledger logic. Only PDF transport + model invocation are backend-specific.

### ✅ Phase 6 — Feishu functionality removed entirely
The original system included a `/write_to_feishu` endpoint and per-week Feishu doc
registration. Per user request, the feature is **gone**:

- `service/query_server.py` — removed all Feishu helpers (`get_feishu_token`, `get_current_week_doc`, `register_week_doc`, `build_search_result_blocks`) and the `/write_to_feishu` route (~177 lines).
- `service/config.py` — removed `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_FOLDER_TOKEN`, `FEISHU_WORKSPACE`, `FEISHU_DOCS_REGISTRY`.
- `.env.example` and `.gitignore` — removed Feishu sections.
- `skills/search-literature/SKILL.md` — removed Step 4 飞书写入, the "保存到飞书" footer hint, and the `/write_to_feishu` endpoint table row. Renumbered the old Step 5 (query log) to Step 4.
- `skills/search-papers/SKILL.md` — removed `/write_to_feishu` endpoint, parameters, and 飞书云文档 section.
- `service/query_server.py` query log frontmatter — removed `feishu_written` and `feishu_document_id` fields from both write and read sides.
- `docs/ARCHITECTURE.md`, `docs/COMPONENTS.md`, `docs/PACKAGING-PLAN.md`, `README.md` — updated endpoint lists and design narratives.

The investigation reports (`docs/investigation/03-,04-,05-*.md`) are kept unchanged
as historical audit artifacts of the original system.

## What's still rough

The systematic evaluation lives in [docs/POLISH-EVALUATION.md](docs/POLISH-EVALUATION.md).
A 6-commit T1+T2 batch landed on 2026-05-08; below is the post-batch state.

### ✅ Resolved in the T1+T2 batch (2026-05-08)

- `--pipeline-mode legacy` removed entirely (commit `86cd17b`, −336 lines)
- Backend factory centralized in `backends/__init__.py:make_backend_from_env` (commit `0693063`)
- `combined_hash` unified on the stable variant via `scanner/_hashing.py` (commit `ac9d174`)
- Sub-agent flow now works end-to-end: `--resume <run_dir>` + 3-invocation pattern (commit `4b11d45`)
- Smoke test suite (15 tests, no SDK required) (commit `659bd62`)
- Bug fixes: `get_parent_key()` collision, stale chunks on PDF re-ingest, Zotero process race (commit `38b8a22`)
- Plus T1+T2 review-fix commit (`<this commit>`): C1 dead exception catch, I1 service-side normalize, I2 stale SKILL flags, I3 dead provider_parts, m4 widened Zotero key regex, m5 --resume warning, R1 cross-module hash parity test.

### ⏳ Remaining rough edges (future Tier-3 work)

- **GCS bucket cleanup not wired** into the scanner main flow. `scanner/cleanup_gcs_archive.py` exists but isn't called by anything. Either schedule it as a cron job or invoke from `scanner/zotero_batch_scanner.py` after a successful batch.
- **Bilingual SKILLs** — most user-facing SKILLs are Chinese-leaning. Public release wants English mirrors (`SKILL.zh.md` + `SKILL.md`). Infra skills already English.
- **Logging refactor** — `print()` everywhere instead of the `logging` module. Hampers test silencing and centralized log control.
- **Docker Compose stack** — Ollama + ChromaDB + query_server in one `docker-compose.yml`. Would close the "I just want to try this" gap.
- **`gemini_analyze_pdf.py` is still ~1990 lines** after the T1+T2 cuts. POLISH-EVALUATION.md §3 sketches a clean split (preflight / routing / rendering / post-publish / runs / pipeline). Worth doing if more features are planned; not urgent for stability.
- **Mocked-SDK tests** for vertex / gemini-api / anthropic / openai backends. Smoke tests currently only cover subagent (the SDK-free one).

## What's deferred (P1 / P2)

### P1 — should fix before community use

- **Fully verify the path-templated code** by actually running it. The edits have been mechanical replacements; I haven't run the test suite (none exists yet).
- **`get_parent_key()` filename substring bug** in `scanner/gemini_analyze_pdf.py` — `WHERE path LIKE '%filename%' LIMIT 1` collides on duplicate basenames. Should switch to attachment item key match.
- **Stale chunks on PDF re-ingest** in `service/build_pdf_db.py` — no `delete_where(group_hash=old_hash)` before `add`. Old chunks accumulate when papers are updated.
- **`combined_hash` algorithm divergence** between `scanner/zotero_batch_scanner.py` (sorted by hash, order-independent) and `service/build_pdf_db.py` (order-preserving). Both ledgers will tolerate the discrepancy, but it's confusing.
- **Cleanup script for GCS bucket** (`scanner/cleanup_gcs_archive.py`) is not wired into the scanner main flow. Should run periodically or on each scan completion.

### P2 — nice to have

- **English SKILL mirrors**. All 4 user-facing SKILLs are still bilingual or Chinese-leaning. For a public release, ship `SKILL.md` (English) + `SKILL.zh.md` (Chinese) per skill. The infra SKILLs (`rag-engineer`, `vector-database-engineer`, `embedding-strategies`) are already English-neutral.
- **Smoke tests** for each query_server endpoint against a tiny seeded ChromaDB.
- **A minimal sample corpus** (3-5 public-domain PDFs + a fake `processed_history.txt`) so new users can validate setup without touching their own Zotero.
- **Docker compose** (`Dockerfile` + `docker-compose.yml`) — Ollama + ChromaDB + query_server in one stack.
- **Pre-commit hooks** (ruff, mypy, secret scanning) — particularly important since the original codebase leaked secrets.
- **CI** — at minimum, lint + import-check on push.

### P0 status — secret rotation (out of repo scope)

🚨 **Independent of this repo**: the original `$LOCALRAG_HOME\upload_*.py` and
`create_doc*.py` scripts (which are NOT in this staging repo and were not modified
by us) contain the original Feishu app secret in plain text. If those files were ever
pushed to a remote git repo, the secret needs to be:

1. Rotated in the Feishu open-platform admin console.
2. Scrubbed from any git history with `git filter-repo`.

Run on any local clone of those originals: `git log --all -S "<REDACTED-ROTATED>"` to verify.

Within `$REPO_ROOT/`, no Feishu code or credential value remains.

## How to verify the staged repo works

A new contributor on a fresh machine should be able to:

> ⚠️ **Historical snapshot (2026-05-08).** The walkthrough below predates the
> single-venv default, the `subagent` default backend, and the 0.6b embedding
> default — several commands no longer match current defaults. Follow the
> **"Bootstrap from a fresh clone"** section in README.md instead.

```bash
git clone <this-repo>
cd research-rag
cp .env.example .env
# edit .env: set GOOGLE_APPLICATION_CREDENTIALS, GOOGLE_CLOUD_PROJECT, GEMINI_VERTEX_GCS_BUCKET, ZOTERO_DB_PATH
./setup.sh
ollama pull qwen3-embedding:4b
# close Zotero
scanner/.venv/bin/python scanner/zotero_batch_scanner.py --limit 5
service/.venv/bin/python service/build_notes_db.py
service/.venv/bin/python service/build_pdf_db.py
service/.venv/bin/python service/query_server.py
```

That sequence has not yet been tested end-to-end.

## What changed vs original (file-level)

If you want to run a diff against the originals:

```bash
diff -r $LOCALRAG_HOME\query_server.py $REPO_ROOT/service\query_server.py
diff -r $LOCALRAG_HOME\build_pdf_db.py $REPO_ROOT/service\build_pdf_db.py
diff -r $LOCALRAG_HOME\build_notes_db.py $REPO_ROOT/service\build_notes_db.py
diff -r $LOCALRAG_HOME\ingest_textbook.py $REPO_ROOT/service\ingest_textbook.py
diff -r scanner/zotero_batch_scanner.py $REPO_ROOT/scanner\zotero_batch_scanner.py
diff -r scanner/gemini_analyze_pdf.py $REPO_ROOT/scanner\gemini_analyze_pdf.py
```

The diffs are confined to:
- The constant block at the top (replaced with imports from `config.py`)
- A few `os.path.join` → string-coerced Path arguments
- argparse defaults for `--zotero-dir` / `--base-dir`
- `print` lines using HOST/PORT instead of literal `127.0.0.1:18810`

Behavior at runtime is identical when the env vars match the original hardcoded
values.
