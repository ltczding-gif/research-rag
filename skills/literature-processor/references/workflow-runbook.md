# Workflow Runbook

Reference for an agent driving `scanner/zotero_batch_scanner.py`. All
paths come from `.env`; no hard-coded values.

## Preflight

Always confirm:

1. Zotero is closed.
2. The user wants one of these scopes:
   - full scan
   - limited validation batch
   - recent additions since a specific date
   - one explicitly provided PDF path

## Environment

Source `.env` (or rely on a wrapper that does). The pipeline reads:

```
LOCALRAG_NOTES_DIR              # vault output
LOCALRAG_PROCESSOR_BACKEND      # vertex / gemini-api / anthropic / openai / subagent
LOCALRAG_DOMAIN_PACK            # which prompt pack to use (default: catalysis)
ZOTERO_DB_PATH                  # zotero.sqlite location
ZOTERO_ATTACHMENT_BASE_DIR      # only when using Zotero linked-file mode

# Vertex backend only:
GOOGLE_APPLICATION_CREDENTIALS  # path to service-account JSON
GOOGLE_CLOUD_PROJECT
GOOGLE_CLOUD_LOCATION
GEMINI_VERTEX_GCS_BUCKET

# Other backends use a single API key each — see .env.example.
```

If you want explicit Python paths instead of relying on PATH:

```
LOCALRAG_MAIN_PYTHON   # absolute path to scanner-side Python
LOCALRAG_RAG_PYTHON    # absolute path to ChromaDB-side Python
```

Rules:

- default model tier is `flash`; the routing policy upgrades to `pro` for long primaries / reviews / dissertations.
- `processed_history.txt` and per-paper `runs/<combined_hash>/` artifacts land under `LOCALRAG_HOME` (default `~/.localrag/`).

## Command Recipes

### A. Full Scan

```bash
python scanner/zotero_batch_scanner.py
```

### B. Limited Validation

```bash
python scanner/zotero_batch_scanner.py --limit N
```

### C. Recent Additions

`--since` is the date boundary. Add `--limit` only if the user explicitly wants a cap.

```bash
python scanner/zotero_batch_scanner.py --since YYYY-MM-DD
```

Date rules:

- "today" / "today added" → use today's date
- "recent" / "last few days" → convert to an actual lower-bound date
- if the user says "latest" but gives no file path, stay in recent-scan mode

### D. Explicit PDF Path

```bash
python scanner/gemini_analyze_pdf.py /path/to/paper.pdf
```

Main + SI:

```bash
python scanner/gemini_analyze_pdf.py /path/to/paper.pdf /path/to/paper_SI.pdf
```

## Canary-First Rule

- write first-pass notes to the canary folder under
  `$LOCALRAG_NOTES_DIR/progress/pipeline_reports/gemini_incremental_alignment/canary_notes/`
- only promote to `$LOCALRAG_NOTES_DIR` after frontmatter validation succeeds

## Post-Generation Ingest

If notes were promoted into the live vault:

```bash
# Ensure Ollama is up — needed for embeddings
curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1 || ollama serve &

python service/build_notes_db.py
python service/build_pdf_db.py

# Restart query server to pick up new data
pkill -f "query_server.py" 2>/dev/null
python service/query_server.py &
```

PowerShell equivalent in the user's `.env`-aware terms:

```powershell
$PYTHON_RAG = $env:LOCALRAG_RAG_PYTHON
try { Invoke-RestMethod http://localhost:11434/api/tags -TimeoutSec 3 | Out-Null }
catch { Start-Process ollama -ArgumentList "serve" -WindowStyle Hidden; Start-Sleep 5 }

& $PYTHON_RAG "$PWD\service\build_notes_db.py"
& $PYTHON_RAG "$PWD\service\build_pdf_db.py"

Get-Process python | Where-Object {$_.CommandLine -like "*query_server*"} | Stop-Process -Force
Start-Sleep 2
Start-Process $PYTHON_RAG -ArgumentList "$PWD\service\query_server.py" -WindowStyle Hidden
```

## Required Result Report

Always tell the user:

- how many items were processed
- how many were skipped
- current note count
- current PDF chunk count
- failures and why
