---
name: gemini-literature-processor
description: >
  Use when the user wants to run `/gemini-literature-processor`,
  batch-scan Zotero PDFs, process one or more paper PDFs with Gemini into
  structured literature notes, repair `processed_history.txt`, or manage this
  workflow's Vertex AI / GCS settings. Triggers: 处理新增论文, 批量生成笔记,
  扫描 Zotero, 生成文献笔记, gemini_analyze_pdf, zotero_batch_scanner,
  verify_and_clean, backfill_hash, cleanup_gcs_archive.
---

# Gemini Literature Processor

The write/ingest side of the literature workflow:

- batch-scan Zotero for new PDFs and generate structured notes
- process one main PDF or a main PDF + SI pair
- repair or audit `processed_history.txt`, `combined_hash`, and ghost/orphan records
- manage the Vertex AI + GCS PDF-input path for this workflow

If the user is asking to **search or read** existing literature, prefer
`search-literature`, `search-notes`, or `search-papers` instead.

Scripts live at `scanner/` in this repo. All paths and credentials are
configured via environment variables — see `.env.example` at the repo root.

## Fast Protocol

1. Confirm Zotero is closed and confirm scope before running anything.
2. Confirm the required env vars are set (Vertex AI auth, GCS bucket — see below).
3. Pick the correct command family:
   - `scanner/zotero_batch_scanner.py` for full / incremental / recent scans
   - `scanner/gemini_analyze_pdf.py` only when the user explicitly provides a PDF path
4. Default new notes to canary output before live-vault promotion.
5. If notes are promoted into `$LOCALRAG_NOTES_DIR`, rebuild notes/PDF DB and restart `query_server.py`.
6. Report processed, skipped, failed, and current corpus counts.

## Backend selection

The note generator runs through a pluggable backend layer. Pick one that matches
what you have credentials for:

| Backend | When to use | Required env |
|---|---|---|
| `subagent` (default) | Zero API key. The host LLM agent (Claude Code, Codex, OpenClaw, …) generates notes via its sub-agent dispatch. See `references/subagent-host-contract.md`. | none |
| `gemini-api` | Simpler cloud setup, no GCP project; just want a Google AI Studio API key | `GEMINI_API_KEY` |
| `anthropic` | You'd rather use Claude than Gemini | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI Inc. or any OpenAI-compatible provider (DeepSeek, Mistral, OpenRouter, Together, Groq, vLLM, Ollama, LM Studio) | `OPENAI_API_KEY` (+ `OPENAI_BASE_URL` for compatible providers) |
| `vertex` | Production GCP setup with a service account; you want PDFs persisted in your own GCS bucket | `GOOGLE_APPLICATION_CREDENTIALS`, `GOOGLE_CLOUD_PROJECT`, `GEMINI_VERTEX_GCS_BUCKET` |

Choose via `LOCALRAG_PROCESSOR_BACKEND` env var or `--backend` flag. Same prompts,
schemas, and template rules feed every backend; only PDF transport and the model
call change.

> ⚠️ **The `openai` backend extracts PDF text locally with `pdfplumber`** before
> sending — it does NOT send the PDF binary. This is the most universally
> portable approach but **figures, tables, and images are lost**. The Vertex,
> Gemini API, and Anthropic backends all send native PDF content, so they can
> reference figures in their analysis. Use `openai` for cost/portability;
> use the other backends for richer figure-aware notes.

## Required Environment (varies by backend)

```bash
export PYTHONUTF8=1                                    # Windows: $env:PYTHONUTF8 = "1"
export LOCALRAG_NOTES_DIR=$HOME/research-note
export ZOTERO_DB_PATH=$HOME/Zotero/zotero.sqlite
export ZOTERO_DATA_DIR=$HOME/Zotero
# Optional: only set if using Zotero linked-file mode
# export ZOTERO_ATTACHMENT_BASE_DIR=/path/to/linked/files

# Pick ONE backend block below:

# --- subagent (default — no API key needed) ---
export LOCALRAG_PROCESSOR_BACKEND=subagent

# --- gemini-api ---
# export LOCALRAG_PROCESSOR_BACKEND=gemini-api
# export GEMINI_API_KEY=AIza...

# --- anthropic ---
# export LOCALRAG_PROCESSOR_BACKEND=anthropic
# export ANTHROPIC_API_KEY=sk-ant-...

# --- openai (or any OpenAI-compatible provider) ---
# export LOCALRAG_PROCESSOR_BACKEND=openai
# export OPENAI_API_KEY=sk-...
# # Compatible providers (uncomment one):
# # export OPENAI_BASE_URL=https://api.deepseek.com/v1     # DeepSeek
# # export OPENAI_BASE_URL=https://openrouter.ai/api/v1    # OpenRouter
# # export OPENAI_BASE_URL=https://api.mistral.ai/v1       # Mistral
# # export OPENAI_BASE_URL=https://api.groq.com/openai/v1  # Groq
# # export OPENAI_BASE_URL=http://localhost:11434/v1       # Ollama
# # Override the model picks (defaults: gpt-4o-mini / gpt-4o):
# # export OPENAI_FLASH_MODEL=deepseek-chat
# # export OPENAI_PRO_MODEL=deepseek-reasoner

# --- vertex (production high-fidelity backend) ---
# export LOCALRAG_PROCESSOR_BACKEND=vertex
# export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
# export GOOGLE_CLOUD_PROJECT=your-gcp-project-id
# export GOOGLE_CLOUD_LOCATION=global
# export GEMINI_VERTEX_GCS_BUCKET=your-gcs-bucket-name
```

The full env-var contract is in `.env.example`. A `.env` file in the repo root
is auto-loaded by the bootstrap scripts; or export manually as above.

Shared defaults baked into code (all backends):
- model tiers: routed by `domain-packs/<active-pack>/config/model_routing_policy.json` (flash vs pro; override via $GEMINI_MODEL_ROUTING_POLICY)
- canonical history ledger: `scanner/processed_history.txt`
- chunked PDF archival path (vertex only): `pdf-inputs/<combined_hash>/`

## Scope Routing

Ask the user these two questions first:

```text
处理前需要确认：
1. Zotero 现在是否已关闭？（必须关闭，否则数据库锁定无法读取）
2. 处理范围是？
   A. 全量扫描（所有未处理论文）
   B. 限量测试（指定数量，建议先跑 10 篇验证）
   C. 只处理最近新增 → 追问：是今天加的，还是某个具体日期之后？
   D. 单篇指定 → 只在用户明确提供了 PDF 路径时才用此选项
```

### Commands

All scripts read paths from env vars; you can omit `--zotero-dir` / `--base-dir`
once `ZOTERO_DATA_DIR` and `ZOTERO_ATTACHMENT_BASE_DIR` are set. Backend is
picked from `$LOCALRAG_PROCESSOR_BACKEND` unless overridden with `--backend`.

#### A — full scan
```bash
python scanner/zotero_batch_scanner.py \
    --out-dir "$LOCALRAG_NOTES_DIR"
```

#### B — limited validation
```bash
python scanner/zotero_batch_scanner.py \
    --out-dir "$LOCALRAG_NOTES_DIR" \
    --limit N
```

#### C — recent / new additions
**Always combine `--since` and `--limit`** for mode C — `--since` alone can
return an unbounded set on busy import days, leading to unexpected API spend.
```bash
python scanner/zotero_batch_scanner.py \
    --out-dir "$LOCALRAG_NOTES_DIR" \
    --since "YYYY-MM-DD" \
    --limit N
```

#### D — explicit PDF path only (user provides path)
```bash
python scanner/gemini_analyze_pdf.py \
    "/path/to/paper.pdf" \
    --out-dir "$LOCALRAG_NOTES_DIR/progress/pipeline_reports/gemini_incremental_alignment/canary_notes"
```

Use `gemini_analyze_pdf.py` with two file paths only when the user explicitly
gives both main and SI PDFs.

#### Switching backend per-invocation

```bash
# Cheap one-off scan with direct Gemini API key
python scanner/zotero_batch_scanner.py --limit 5 \
    --backend gemini-api

# Try Claude on a single paper
python scanner/gemini_analyze_pdf.py "/path/to/paper.pdf" \
    --backend anthropic

# Use a local model via Ollama's OpenAI-compatible endpoint
OPENAI_BASE_URL=http://localhost:11434/v1 OPENAI_API_KEY=ollama \
OPENAI_FLASH_MODEL=qwen2.5:14b OPENAI_PRO_MODEL=qwen2.5:32b \
python scanner/gemini_analyze_pdf.py "/path/to/paper.pdf" \
    --backend openai
```

#### Sub-agent mode (no external API call)

If `LOCALRAG_PROCESSOR_BACKEND=subagent` (or `--backend subagent`), the
scanner doesn't call any remote LLM. It writes a **manifest JSON** describing
what a sub-agent must do, then exits with code **200** (= "pending sub-agent",
distinct from 0/success and 1/failure). The parent agent dispatches a fresh
sub-agent against the manifest, the sub-agent writes a structured JSON
output, and the parent re-runs the scanner — which auto-resumes.

**This works on any host platform**, not just Claude Code: see
[`references/subagent-host-contract.md`](references/subagent-host-contract.md)
for Codex / OpenClaw / generic LLM bindings, the manifest schema, and the
exit-code contract.

**Single paper — three invocations** (Stage B's prompt depends on Stage A's
output, so the orchestrator can't emit both manifests at once):

```bash
# Invocation 1 — emits Stage A (Document Profiler) manifest
python scanner/gemini_analyze_pdf.py /path/to/paper.pdf --backend subagent
#   → exits 200, writes runs/<combined_hash>/manifest-profiler.json

# Dispatch a sub-agent with this prompt:
#   "Read the manifest at <manifest_path>. Follow its subagent_task block:
#    read each PDF in pdf_paths, apply system_prompt + user_prompt, produce
#    a JSON object conforming to response_schema, write to
#    expected_output_path. Do not re-invoke the scanner."
# In Claude Code: Task tool. In Codex: nested `codex exec`. In OpenClaw: its
# equivalent. The sub-agent writes runs/<combined_hash>/01-document-profile.json.

# Invocation 2 — Stage A loaded from disk; emits Stage B manifest
python scanner/gemini_analyze_pdf.py /path/to/paper.pdf \
    --backend subagent --resume <run_dir>
#   → exits 200, writes runs/<combined_hash>/manifest-note_generator.json

# Dispatch sub-agent again against manifest-note_generator.json.
# It writes runs/<combined_hash>/02-note-draft.json.

# Invocation 3 — both stages loaded; renders the final note
python scanner/gemini_analyze_pdf.py /path/to/paper.pdf \
    --backend subagent --resume <run_dir>
#   → exits 0, writes the final _review_note.md and updates the ledger
```

`--resume` is idempotent: if both stages are already filled, invocations 2
and 3 collapse into a single rendering pass.

**Batch — same loop, run on `zotero_batch_scanner.py`:**

```bash
python scanner/zotero_batch_scanner.py --limit 5 --backend subagent
# exit 200 → manifests written for the batch. The scanner prints which
#            run_dirs are pending, plus the dispatch prompt for the host.

# Dispatch one sub-agent per pending manifest. Discover them via:
python scanner/list_pending_subagent_runs.py --json
# → JSON list with manifest_path / expected_output_path / pdf_paths /
#   combined_hash for each pending run.

# Re-run the same batch command. The scanner auto-resumes any group whose
# run_dir already exists, advancing it to the next stage.
python scanner/zotero_batch_scanner.py --limit 5 --backend subagent
# Loop until exit 0.
```

Each paper still takes 3 passes (Stage A manifest → fill → Stage B manifest
→ fill → final render). The batch wrapper just runs all N papers
concurrently per pass, so wall-clock time is `~3 × longest_subagent_call`
not `3 × N`.

**When to use:** zero-API setup; experimenting on a small set; running on a
host that can't (or won't) hold an API key. For unattended high-volume
production batches, prefer `vertex` / `gemini-api` / `anthropic` / `openai`.

## Live-Vault Admission

Use canary output first:
- `$LOCALRAG_NOTES_DIR/progress/pipeline_reports/gemini_incremental_alignment/canary_notes/`

Only allow live-vault promotion after deterministic prefill and frontmatter
validation pass.

Candidate-first note rules:
- keep final `tags: []` empty during machine generation
- fill `candidate_tags_high`, `candidate_tags_medium`, `candidate_tags_low`
- set `human_reviewed: 0`
- preserve `combined_hash`, `zotero_parent_key`, `seed_terms`, `scope_hint`, and `signal_quality`
- do not write `tag_review_status` into final note frontmatter

## Post-Generation Ingest

If notes were promoted into `$LOCALRAG_NOTES_DIR`, run:

### Unix / macOS
```bash
# 1. Ensure Ollama is running
curl -sf http://localhost:11434/api/tags >/dev/null || (ollama serve &)
sleep 2

# 2. Rebuild ChromaDB collections
python service/build_notes_db.py
python service/build_pdf_db.py

# 3. Restart query server
pkill -f query_server.py 2>/dev/null
sleep 1
python service/query_server.py &
```

### Windows (PowerShell)
```powershell
try {
    Invoke-RestMethod http://localhost:11434/api/tags -TimeoutSec 3 | Out-Null
} catch {
    Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep 5
}

python service\build_notes_db.py
python service\build_pdf_db.py

Get-Process python | Where-Object {$_.CommandLine -like "*query_server*"} | Stop-Process -Force
Start-Sleep 2
Start-Process python -ArgumentList "service\query_server.py" -WindowStyle Hidden
```

Report:
- processed count
- skipped count
- total note count
- total PDF chunk count
- failed items and why

## Maintenance Tools

| Tool | Purpose |
|------|---------|
| `scanner/verify_and_clean.py` | Reconcile `processed_history.txt` with the live note vault; identify ghost / orphan records |
| `scanner/backfill_hash.py` | Add `combined_hash` to old notes that pre-date the in-frontmatter hash field |
| `scanner/cleanup_gcs_archive.py` | Trim old GCS objects from `pdf-inputs/` |

## Detailed References

Read these only when needed:
- `references/workflow-runbook.md`
- `references/incremental-note-contract.md`
- `references/maintenance-tools.md`
