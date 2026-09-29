# 05 — Packaging & Portability Analysis

**Date**: 2026-05-08  
**Auditor**: Claude Code (Sonnet 4.6)  
**Scope**: Release readiness audit for the local literature RAG system, covering `$LOCALRAG_HOME\`, `skills/gemini-literature-processor\`, and related skills under `.claude\skills\`.

---

## 1. Release Blocker Matrix

| # | Blocker | Severity | Location (file:line) | Proposed Fix |
|---|---------|----------|---------------------|--------------|
| B1 | `FEISHU_APP_SECRET` literal `<REDACTED-ROTATED>` committed in plain text | **P0** | `batch_create.py:10`, `analyze_folder.py:10`, `batch_trash.py:9`, `check_doc.py:10`, `check_status.py:9`, `create_doc2.py:10`, `check_feishu.py:10`, `create_docs.py:16`, `create_docs_v2.py:15`, `create_docs_v3.py:15`, `fix_doc_content.py:15`, `delete_duplicates.py:15` | Replace with `os.environ.get("FEISHU_APP_SECRET")` and revoke/rotate the current secret before publishing |
| B2 | `FEISHU_APP_ID` literal `<REDACTED-APP-ID>` in same files | **P0** | Same files as B1 | Replace with `os.environ.get("FEISHU_APP_ID")` |
| B3 | Feishu workspace subdomain `<your-workspace>.feishu.cn` hard-coded in print statements | **P0** | `create_docs.py:148`, `batch_create.py:70`, `create_doc2.py:104`, `create_docs_v2.py:170`, `create_docs_v3.py:103,106`, `fix_doc_content.py:109`, `write_doc_content.py:79`, `delete_duplicates.py:84`, `verify_upload.py:75` | Move to `FEISHU_WORKSPACE` env var |
| B4 | Feishu folder token `<REDACTED-FOLDER-TOKEN>` hard-coded | **P0** | `batch_create.py:11`, `analyze_folder.py:11`, `check_doc.py:11`, `create_doc2.py:11` | Move to `FEISHU_FOLDER_TOKEN` env var |
| B5 | Feishu document ID `<doc-id>` in `feishu_docs.json` | **P0** | `.localrag\feishu_docs.json` | This file is user-state — add to `.gitignore`, ship only `feishu_docs.json.example` with empty `{}` |
| B6 | `CHROMA_PATH = r"$LOCALRAG_HOME\chroma"` hard-coded in core service | **P0** | `query_server.py:21`, `build_pdf_db.py:94`, `build_notes_db.py` (implied), `ingest_textbook.py:121` | Replace with `os.environ.get("LOCALRAG_CHROMA_PATH", ...)` |
| B7 | `ZOTERO_DB = r"$ZOTERO_DB_PATH"` hard-coded | **P0** | `build_pdf_db.py:27`, `gemini_analyze_pdf.py:23` | Replace with `os.environ.get("ZOTERO_DB_PATH")` |
| B8 | `ZOTERO_DB` default `$ZOTERO_DATA_DIR` in scanner CLI default | **P0** | `zotero_batch_scanner.py:401` | Keep as `argparse` default but document the env var override |
| B9 | `NOTES_DIR = r"$LOCALRAG_NOTES_DIR"` (different drive letter) hard-coded | **P0** | `build_pdf_db.py:57`, `query_server.py:3` (docstring), `gemini_analyze_pdf.py:36` | Replace with `os.environ.get("LOCALRAG_NOTES_DIR")` |
| B10 | `QUERY_LOG_ROOT = r"$LOCALRAG_NOTES_DIR\_query_logs"` hard-coded | **P1** | `query_server.py:33` | Replace with `os.environ.get("LOCALRAG_QUERY_LOG_ROOT", ...)` |
| B11 | `FEISHU_DOCS_REGISTRY = r"$LOCALRAG_HOME\feishu_docs.json"` hard-coded | **P1** | `query_server.py:30` | Replace with `os.environ.get("LOCALRAG_FEISHU_REGISTRY", ...)` |
| B12 | `CANONICAL_SKILL_ROOT` defaults to `$HOME\.agents\skills\...` | **P1** | `gemini_analyze_pdf.py:25-27` | Already reads env var `GEMINI_LITERATURE_SKILL_ROOT`; SKILL.md still pins absolute path — update SKILL.md |
| B13 | `$ZOTERO_ATTACHMENT_BASE_DIR` as default `--base-dir` in scanner | **P1** | `zotero_batch_scanner.py:402` | Change argparse default to `None`; document env var `ZOTERO_ATTACHMENT_BASE_DIR` |
| B14 | All SKILL.md files bake `$LOCALRAG_MAIN_PYTHON` as the literal Python path | **P1** | All skill SKILL.md files (`.claude\skills\`, `.agents\skills\`, `.openclaw\skills\`) | Replace literal paths with a configurable variable or rely on `$env:LOCALRAG_PYTHON311` per SKILL setup instructions |
| B15 | `APPROVED_MAIN_PYTHON` and `APPROVED_RAG_PYTHON` hard-coded as `Path` literals | **P1** | `gemini_analyze_pdf.py:37-38` | Read from env vars `LOCALRAG_MAIN_PYTHON` and `LOCALRAG_RAG_PYTHON` |
| B16 | ~30 Feishu upload experiment scripts (`upload_*.py`, `create_docs*.py`, etc.) contain user-specific data (paper filenames, workspace URLs) and are clearly not production code | **P1** | Entire `.localrag\upload_*.py`, `create_docs*.py`, `fix_doc_content.py`, `write_doc_content.py`, etc. | Move to a `_dev_experiments/` sub-folder that is not part of the plugin package; do not ship in the open-source bundle |
| B17 | Chinese-language UX baked into all three search skills and gemini-literature-processor (instruction text, step prompts, error tables) | **P2** | `.claude\skills\search-literature\SKILL.md`, `search-notes\SKILL.md`, `search-papers\SKILL.md`, `gemini-literature-processor\SKILL.md` | Add English-language mirror SKILL files for the open-source version; canonical Chinese versions remain for personal use |
| B18 | Vertex AI project `<your-gcp-project>` and GCS bucket `<your-gcs-bucket>` embedded in SKILL.md | **P1** | `.agents\skills\gemini-literature-processor\SKILL.md:58-60` | Move to `GOOGLE_CLOUD_PROJECT` and `GEMINI_VERTEX_GCS_BUCKET` env vars (already in code, just not in SKILL.md instructions) |
| B19 | `APPROVED_MAIN_PYTHON` path in `gemini_analyze_pdf.py` contains user home path | **P1** | `gemini_analyze_pdf.py:37` | See B15 |
| B20 | Port 18810 for query server and 18800 referenced in some SKILL.md files (inconsistency) | **P2** | `query_server.py:1122`, `search-notes\SKILL.md:35`, `search-papers\SKILL.md:29` vs `search-literature\SKILL.md` using 18810 | Standardize to one port; expose as `LOCALRAG_PORT` env var; fix SKILL.md inconsistency |

---

## 2. Hard-coded Path Inventory

### 2a. User Home (`$HOME\`)

| Path | File(s) | Proposed Config Key |
|------|---------|---------------------|
| `$LOCALRAG_HOME\chroma` | `query_server.py:21`, `build_pdf_db.py:94`, `ingest_textbook.py` | `LOCALRAG_CHROMA_PATH` |
| `$LOCALRAG_HOME\feishu_docs.json` | `query_server.py:30` | `LOCALRAG_FEISHU_REGISTRY` |
| `$LOCALRAG_HOME\processed_groups.txt` | `build_pdf_db.py:96` | `LOCALRAG_PDF_LEDGER` |
| `$LOCALRAG_MAIN_PYTHON` | `gemini_analyze_pdf.py:37`, all SKILL.md files | `LOCALRAG_MAIN_PYTHON` |
| `$LOCALRAG_RAG_PYTHON` | `gemini_analyze_pdf.py:38`, all SKILL.md files | `LOCALRAG_RAG_PYTHON` |
| `ollama` | SKILL.md files | `LOCALRAG_OLLAMA_EXE` |
| `skills/gemini-literature-processor` | `gemini_analyze_pdf.py:25-27` (env var fallback) | `GEMINI_LITERATURE_SKILL_ROOT` (already env-var-able) |
| `$HOME\<your-service-account>.json` | SKILL.md:56 | `GOOGLE_APPLICATION_CREDENTIALS` (standard Google env var) |

### 2b. Zotero Data Directory (`$ZOTERO_DATA_DIR`)

| Path | File(s) | Proposed Config Key |
|------|---------|---------------------|
| `$ZOTERO_DB_PATH` | `build_pdf_db.py:27`, `gemini_analyze_pdf.py:23` | `ZOTERO_DB_PATH` |
| `$ZOTERO_DATA_DIR` (directory) | `zotero_batch_scanner.py:401` (argparse default) | `ZOTERO_DATA_DIR` |

### 2c. PDF Attachment Base (`$ZOTERO_ATTACHMENT_BASE_DIR`)

| Path | File(s) | Proposed Config Key |
|------|---------|---------------------|
| `$ZOTERO_ATTACHMENT_BASE_DIR` | `zotero_batch_scanner.py:402` (argparse default) | `ZOTERO_ATTACHMENT_BASE_DIR` |

### 2d. Notes Output (`$LOCALRAG_NOTES_DIR`)

| Path | File(s) | Proposed Config Key |
|------|---------|---------------------|
| `$LOCALRAG_NOTES_DIR` | `build_pdf_db.py:57`, `gemini_analyze_pdf.py:36`, `search-notes\SKILL.md:14` | `LOCALRAG_NOTES_DIR` |
| `$LOCALRAG_NOTES_DIR\_query_logs` | `query_server.py:33` | `LOCALRAG_QUERY_LOG_ROOT` |
| `$LOCALRAG_NOTES_DIR\progress\pipeline_reports\gemini_incremental_alignment` | `gemini_analyze_pdf.py:30-33` (env var fallback) | `GEMINI_INCREMENTAL_ALIGNMENT_REPORT_ROOT` (already env-var-able) |

### 2e. Model / Service URLs

| Value | File(s) | Proposed Config Key |
|-------|---------|---------------------|
| `http://localhost:11434/api/embeddings` | `build_pdf_db.py:153`, `build_notes_db.py:26`, `query_server.py:51,88,560`, `ingest_textbook.py:121`, `query_server_v2.py:27,127` | `OLLAMA_EMBED_URL` |
| `http://127.0.0.1:18810` | `query_server.py:1122`, `search-literature\SKILL.md` | `LOCALRAG_PORT` (default 18810) |
| `http://127.0.0.1:18800` | `search-notes\SKILL.md:35`, `search-papers\SKILL.md:29` (stale port) | Fix to match `LOCALRAG_PORT` |

---

## 3. Secrets / Credentials Audit

| Secret | Status | Location | Action |
|--------|--------|----------|--------|
| `FEISHU_APP_SECRET = "<REDACTED-ROTATED>"` | **LEAKED** (plain text) | 12 files in `.localrag\` | Rotate immediately. Replace with `os.environ.get("FEISHU_APP_SECRET")`. Add to `.gitignore` pre-check. |
| `FEISHU_APP_ID = "<REDACTED-APP-ID>"` | **LEAKED** (app ID, lower risk but still PII) | Same 12 files | Replace with `os.environ.get("FEISHU_APP_ID")` |
| `FEISHU_FOLDER_TOKEN = "<REDACTED-FOLDER-TOKEN>"` | **LEAKED** (drive token) | 4 files | Move to env var or `feishu_docs.json` user-state file |
| Feishu document IDs in `feishu_docs.json` | **User-state** | `.localrag\feishu_docs.json` | gitignore; ship `feishu_docs.json.example` |
| `FEISHU_APP_SECRET` in `query_server.py:29` | **Safe** — uses `os.environ.get(...)` | `query_server.py` | No action needed; this is the correct pattern |
| `GEMINI_API_KEY` | **Not found in code** — correctly read from env | SKILL.md mentions `$env:GEMINI_API_KEY` | Safe. Document as required env var in `.env.example`. |
| `GOOGLE_APPLICATION_CREDENTIALS` | Not in code; SKILL.md sets it at runtime | SKILL.md:56 | Safe. Document path convention in `.env.example`. |
| Vertex AI project ID `<your-gcp-project>` | In SKILL.md only, not code | SKILL.md:58 | Move to env var `GOOGLE_CLOUD_PROJECT` in docs |

**Pre-release action**: Run `git log --all -S "<REDACTED-ROTATED>"` to verify the secret was never committed to git history; if it was, rewrite history with `git filter-repo` before publishing.

---

## 4. Suggested Config Schema

Proposed `.env.example` — the contract a new user fills in:

```dotenv
# ============================================================
# LocalRAG / Zotero-Claude-RAG Configuration
# Copy to .env and fill in your values.
# ============================================================

# --- Python executables ---
# Python 3.11 for note generation, Gemini, Zotero scanning
LOCALRAG_MAIN_PYTHON=/usr/bin/python3.11
# Python in the isolated ChromaDB venv (created by setup.sh)
LOCALRAG_RAG_PYTHON=./.localrag/venv/bin/python

# --- Path layout ---
# Root where generated literature notes are stored
LOCALRAG_NOTES_DIR=~/research-note
# ChromaDB persistent data directory
LOCALRAG_CHROMA_PATH=~/.localrag/chroma
# PDF ingestion ledger (tracks which PDF groups have been embedded)
LOCALRAG_PDF_LEDGER=~/.localrag/processed_groups.txt
# Notes ingestion ledger
LOCALRAG_NOTES_LEDGER=~/.localrag/processed_notes.txt
# Query log root
LOCALRAG_QUERY_LOG_ROOT=~/research-note/_query_logs

# --- Zotero ---
# Full path to zotero.sqlite (close Zotero before running scans)
ZOTERO_DB_PATH=~/Zotero/zotero.sqlite
# Zotero data directory (parent of storage/ and zotero.sqlite)
ZOTERO_DATA_DIR=~/Zotero
# Base path for linked-file attachments (if using linked-file mode)
# Leave blank if using Zotero's default storage/ layout
ZOTERO_ATTACHMENT_BASE_DIR=

# --- Ollama embedding service ---
OLLAMA_EMBED_URL=http://localhost:11434/api/embeddings
# Embedding model (must be pulled: ollama pull qwen3-embedding:4b)
OLLAMA_EMBED_MODEL=qwen3-embedding:4b
# Ollama executable path (Windows only; on Unix use PATH)
# LOCALRAG_OLLAMA_EXE=C:\Users\...\ollama.exe

# --- Query server ---
LOCALRAG_PORT=18810

# --- Gemini / Google Cloud ---
# Required for note generation (API-key mode)
GEMINI_API_KEY=AIza...
# OR use Vertex AI (service-account mode; provide all three)
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=global
GEMINI_VERTEX_GCS_BUCKET=your-gcs-bucket-name

# --- Feishu / Lark (optional) ---
FEISHU_APP_ID=cli_...
FEISHU_APP_SECRET=...
FEISHU_FOLDER_TOKEN=...
# Your Feishu workspace subdomain (e.g. "your-company" for your-company.feishu.cn)
FEISHU_WORKSPACE=your-company

# --- Skill root (advanced) ---
GEMINI_LITERATURE_SKILL_ROOT=~/.agents/skills/gemini-literature-processor
```

---

## 5. Suggested Plugin / Repo Layout

```
zotero-claude-rag/                         ← repo root
│
├── .env.example                           ← config contract (section 4)
├── setup.sh  /  setup.ps1                ← bootstrap: create venv, pip install, ollama pull
├── requirements-rag.txt                   ← chromadb==1.5.5, pdfplumber, flask, pyyaml
├── requirements-gemini.txt                ← google-genai, pdfplumber, pyyaml
│
├── service/                               ← the sidecar HTTP server (NOT a skill)
│   ├── query_server.py                    ← main entrypoint; reads all paths from env
│   ├── build_pdf_db.py                    ← ingestion: PDF → ChromaDB
│   ├── build_notes_db.py                  ← ingestion: notes → ChromaDB
│   └── ingest_textbook.py                 ← large-document batched ingestion
│
├── scanner/                               ← note generation pipeline
│   ├── zotero_batch_scanner.py
│   ├── gemini_analyze_pdf.py
│   ├── verify_and_clean.py
│   ├── backfill_hash.py
│   └── cleanup_gcs_archive.py
│
├── skills/                                ← Claude Code skill contracts
│   ├── search-literature/
│   │   ├── SKILL.md                       ← English version (for open-source)
│   │   └── SKILL.zh.md                    ← Chinese version (canonical personal use)
│   ├── search-notes/
│   │   ├── SKILL.md
│   │   └── SKILL.zh.md
│   ├── search-papers/
│   │   ├── SKILL.md
│   │   └── SKILL.zh.md
│   ├── gemini-literature-processor/
│   │   ├── SKILL.md
│   │   ├── SKILL.zh.md
│   │   └── references/
│   │       ├── workflow-runbook.md
│   │       ├── incremental-note-contract.md
│   │       └── maintenance-tools.md
│   ├── rag-engineer/
│   │   └── SKILL.md                       ← already language-neutral; ship as-is
│   ├── vector-database-engineer/
│   │   └── SKILL.md                       ← already language-neutral; ship as-is
│   └── embedding-strategies/
│       └── SKILL.md                       ← already language-neutral; ship as-is
│
├── config/
│   └── model_routing_policy.json          ← shipped; contains no secrets
│
└── .gitignore
```

**What stays out of the repo:**
- `service/venv/` (generated by `setup.sh`)
- `service/chroma/` (user database)
- `scanner/processed_history.txt`, `service/processed_groups.txt`, `service/processed_notes.txt` (user state)
- `service/feishu_docs.json` (user state)
- `$LOCALRAG_NOTES_DIR\` actual notes (user content)
- All `upload_*.py`, `create_docs*.py` experiment scripts (user-specific one-offs)
- `.env` (credentials)
- `vertex-ai-*.json` (service account key)

The Feishu upload experiment scripts (30+ files) are personal tooling used during development. They contain the leaked credentials and should not be part of the released package at all.

---

## 6. Cross-Platform Plan

### Genuinely Windows-only dependencies

| Item | Why Windows-specific | Cross-platform equivalent |
|------|---------------------|--------------------------|
| `Start-Process ... -WindowStyle Hidden` | PowerShell-only background process | `subprocess.Popen(..., start_new_session=True)` or a systemd/launchd service file |
| `$OLLAMA = "C:\...\ollama.exe"` | Executable path | `ollama` on Unix is in `$PATH`; use env var or `shutil.which("ollama")` |
| `netstat -ano \| findstr 18810` | Windows-specific | `ss -tlnp \| grep 18810` on Linux, `lsof -i :18810` on macOS |
| `Get-Process python \| Where-Object ...` | PowerShell | `pkill -f query_server.py` |
| Backslash paths in all SKILL.md files | Windows convention | Use forward slashes in docs; Python `pathlib.Path` handles both |
| Zotero data at `$ZOTERO_DATA_DIR` | Windows default install | macOS: `~/Zotero`; Linux: `~/Zotero`; configurable via `ZOTERO_DATA_DIR` |
| Python at `C:\Users\...\Python311\python.exe` | Windows-specific | Linux/macOS: `python3.11` via pyenv or system; configurable via `LOCALRAG_MAIN_PYTHON` |
| Drive letters (`D:\`, `E:\`, `F:\`) | NTFS multi-drive | All paths must go through env vars |

### What is cross-platform by nature

- ChromaDB 1.5.5 with Rust bindings: ships wheels for Linux/macOS/Windows.
- Ollama: available on all three platforms.
- The Python scripts themselves (once paths are env-var-ized): pure Python, no OS-specific syscalls.
- Flask query server: cross-platform.
- Zotero: available on all three platforms (different default data dirs).

### Cross-platform setup action

Replace all PowerShell-specific service-management commands in SKILL.md with Python one-liners or a `service/start.py` script. Keep the PowerShell steps as `<!-- Windows -->` comments for Windows users. Add a `service/start.sh` for Unix.

---

## 7. Open vs. Private Content

| File | Content | Recommendation |
|------|---------|----------------|
| `processed_history.txt` (1183 SHA256 hashes) | Hashes of user's PDFs — no PII per se, but fingerprints of personal library | **gitignore** — ship empty template |
| `processed_groups.txt` (1121 hashes) | Same | **gitignore** |
| `processed_notes.txt` | Notes build ledger | **gitignore** |
| `feishu_docs.json` | User's Feishu doc IDs | **gitignore** — ship `feishu_docs.json.example` with `{}` |
| `config/model_routing_policy.json` | Pipeline routing logic, no user data | **Ship it** |
| `references/workflow-runbook.md` etc. | Generic workflow docs | **Ship them** after removing any personal paths that may have crept in |
| `$LOCALRAG_NOTES_DIR\` | 794 actual literature notes | **Never in repo** — clearly excluded |
| `chroma/` database | Embeddings of user's research | **Never in repo** |
| `.env` / `vertex-ai-*.json` | Credentials | **Never in repo** |
| All `upload_*.py` / `create_docs*.py` | Feishu experiment scripts with leaked secrets | **Never in repo** — delete from history if previously committed |

---

## 8. Documentation Needed for a New User

### Bootstrap Guide (ordered steps)

1. **Prerequisites**: Python 3.11, Git, Ollama installed, Zotero installed with a library.
2. **Clone repo** and copy `.env.example` → `.env`; fill in all `REQUIRED` fields.
3. **Create ChromaDB venv**: `./setup.sh` (or `setup.ps1` on Windows) — creates `.localrag/venv` with `chromadb==1.5.5`.
4. **Pull embedding model**: `ollama pull qwen3-embedding:4b`.
5. **Configure Gemini**: Set `GEMINI_API_KEY` (API-key mode) or `GOOGLE_APPLICATION_CREDENTIALS` + `GOOGLE_CLOUD_PROJECT` (Vertex AI mode).
6. **First scan**: Close Zotero, then run `zotero_batch_scanner.py --limit 5` to validate.
7. **Build vector databases**: Run `build_notes_db.py` then `build_pdf_db.py`.
8. **Start query server**: `python service/query_server.py` (or use the provided start script).
9. **Install skills**: Copy the `skills/` directory into your `.claude/skills/` folder.
10. **Test**: Ask Claude Code "what papers do you know about CO2 reduction?".

### Troubleshooting Reference

| Symptom | Cause | Fix |
|---------|-------|-----|
| `database is locked` | Zotero still running | Close Zotero; retry |
| `quota exceeded` / `429` | Gemini daily quota hit | Use `--api-keys "KEY1,KEY2"` to rotate |
| `FileNotFoundError` on PDF | Linked attachment path wrong | Set `ZOTERO_ATTACHMENT_BASE_DIR` |
| YAML parse error in note | Colon in title field | Run `gemini_analyze_pdf.py --force` on that paper |
| `Port 18810 already in use` | Old server process | `pkill -f query_server.py` or `kill $(lsof -t -i:18810)` |
| ChromaDB init fails | Wrong Python (not the venv) | Use `LOCALRAG_RAG_PYTHON` |
| Empty retrieval results | Embeddings not built | Re-run `build_notes_db.py` / `build_pdf_db.py` |

---

## 9. i18n Recommendation

**Recommendation: keep Chinese as canonical for SKILLs, add English mirrors.**

The system's domain language is electrochemistry / interface chemistry literature, which is predominantly cited in English but discussed by the user in Chinese. The Chinese SKILL.md files have been refined through real use; they contain nuanced workflow rules that would lose precision in translation.

The practical approach:
- Ship `SKILL.zh.md` (Chinese, canonical) alongside `SKILL.md` (English mirror) in each skill directory.
- The English `SKILL.md` is the public-facing file that open-source users get; it should use English examples but keep Chinese paper titles as-is (they are just data).
- The three support skills (`rag-engineer`, `vector-database-engineer`, `embedding-strategies`) are already written in language-neutral English and require no changes.
- Do not attempt to translate the actual literature note content or example paper names — those are user data, not UX strings.

---

## 10. Recommended Naming

### Candidate 1: `zotero-claude-rag`
- **Pros**: Explicit about the two main components (Zotero as source, Claude Code as interface, RAG as the mechanism). Searchable. Makes the dependency graph obvious.
- **Cons**: Long; "claude" in a community plugin name may conflict with Anthropic branding guidelines.
- **Plugin slug**: `zotero-rag`

### Candidate 2: `localrag`
- **Pros**: Already the working name of the service layer; short, generic, reusable for non-Zotero corpora.
- **Cons**: Too generic — does not communicate the Zotero + Claude Code specificity.
- **Plugin slug**: `localrag`

### Candidate 3: `research-rag` (recommended)
- **Pros**: Describes the use case (research literature) without over-specifying the source (works for Zotero, Mendeley, or a folder of PDFs). Neutral enough not to step on Anthropic branding. Short. Clear.
- **Cons**: Slightly generic; "research" is a large space.
- **Plugin slug**: `research-rag`

**Recommendation: use `research-rag` as the repo name and plugin slug.** The README intro line can be "A Claude Code plugin for local-first literature RAG over Zotero PDF libraries" — that communicates the specialization without baking it into the name.

---

## Appendix: Skill Duplication Resolution

Four copies of `gemini-literature-processor/SKILL.md` exist:

| Location | Lines | Status |
|----------|-------|--------|
| `.agents\skills\gemini-literature-processor\SKILL.md` | 181 | **Canonical** — references `.agents\` paths, includes Vertex AI, GCS, pipeline-mode commands, references/ dir |
| `.claude\skills\gemini-literature-processor\SKILL.md` | 230 | **Outdated** — references `.openclaw\` paths, older API-key-only workflow, no GCS/Vertex section |
| `.openclaw\skills\gemini-literature-processor\SKILL.md` | (same as .claude copy) | Mirror |
| `.cc-switch\skills\gemini-literature-processor\SKILL.md` | (likely same) | Mirror |

**Action**: The `.agents\skills\gemini-literature-processor\` version is the canonical one. The `.claude\`, `.openclaw\`, and `.cc-switch\` copies should be replaced with a single-line redirect: `See canonical: skills/gemini-literature-processor\SKILL.md`. In the open-source repo, ship only the `.agents\` version.

The three engineering support skills (`rag-engineer`, `vector-database-engineer`, `embedding-strategies`) exist only under `.claude\skills\` and contain no duplication. They should ship as-is in the bundle.
