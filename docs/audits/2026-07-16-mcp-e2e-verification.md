# MCP retrieval path — real-venv end-to-end verification

**Date:** 2026-07-16
**Scope:** ADR-001 P1 Action Item 7 — validate the stdio MCP retrieval path
end-to-end under real dependencies (chromadb + mcp + fastembed), on the
maintainer's machine, in a fresh `.venv`.
**Verdict:** PASS (8/8 E2E checks) after fixing one blocking bug in
`query_server.py` startup. This is a single-machine, single-OS run; a
cross-OS clean-room bootstrap is still outstanding (see Known limitations).

---

## Environment

| Item | Value |
|---|---|
| OS | Windows 11 (10.0.26200) |
| Python (venv) | 3.11.9 |
| venv location | `<repo>/.venv` (built with Python 3.11) |
| chromadb | 1.5.9 |
| mcp | 1.28.1 |
| fastembed | 0.8.0 |
| onnxruntime | 1.27.0 |
| flask | 3.1.3 |
| pypdf | 6.14.2 |
| pdfplumber | 0.11.10 |
| pyyaml | 6.0.3 |
| Embedding provider | `fastembed` (default) |
| Embedding model | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384-dim; ~0.22 GB, downloaded on first build) |

The `mcp` package does not expose `__version__`; version taken from the pip
install manifest (`mcp-1.28.1`).

---

## pytest: before vs. after installing service deps

The service-tier tests (`test_mcp_server.py`, the chromadb branch of
`test_doctor.py`) were `pytest.importorskip`-guarded and had never run under
real dependencies. Both runs used isolated `LOCALRAG_HOME` / `LOCALRAG_NOTES_DIR`
/ `LOCALRAG_QUERY_LOG_ROOT` in a temp dir with `LOCALRAG_EMBED_PROVIDER=fastembed`.

| Run | Interpreter | Result |
|---|---|---|
| **Before** | system `Python311` (no chromadb/flask/fastembed) | **152 passed, 2 skipped, 0 failed** |
| **After** | `.venv` (full deps) | **154 passed, 0 skipped, 0 failed** |

**Delta: 2 skip → pass, 0 new failures.** The two tests that transitioned:

- `tests/test_mcp_server.py::test_mcp_server_registers_expected_tools`
  (was skipped: chromadb + flask missing) → **pass**
- `tests/test_doctor.py::test_check_chromadb_version_accepts_any_1_x`
  (was skipped: chromadb missing) → **pass**

No test that previously passed regressed after the two source fixes below
(re-ran full suite post-fix: 154 passed).

---

## End-to-end stdio round-trip

Tool: **`scripts/verify_mcp_e2e.py`** (committed, re-runnable).

**Method.** The script spawns the MCP server as a subprocess through
`scripts/run_mcp_server.py` using a **non-venv Python** (system
`Python311`), while the client runs under `.venv`. Because chromadb /
fastembed / mcp exist **only** in `.venv`, the server can only succeed if
the launcher's re-exec-into-`.venv` logic actually fires — so a green run is
itself proof that `.mcp.json`'s `command: "python"` design works.

The corpus is 3 synthetic research notes (isolated temp `LOCALRAG_NOTES_DIR`),
each on a distinct electrochemistry topic with real-schema frontmatter
(`zotero_parent_key`, `title_en/zh`, `year`, `journal`, `doi`, `authors`):

- `oer_iridium_review_note.md` — oxygen **evolution** (析氧, OER)
- `co2rr_copper_review_note.md` — CO2 reduction (CO2RR)
- `pemfc_ptco_review_note.md` — fuel cell / oxygen **reduction** (氧还原, ORR)

The query `"氧析出反应催化剂"` (oxygen *evolution* catalyst) is deliberately
close to the fuel-cell note's 氧*还原* (oxygen *reduction*) topic, so an
OER-first ranking demonstrates genuine semantic discrimination.

**Results (8/8 PASS):**

| # | Check | Result |
|---|---|---|
| 1 | `list_tools` exposes `search_notes` / `search_papers` / `get_note` / `index_status` | PASS |
| 2 | `index_status.notes_ready == true` (note_count=3) | PASS |
| 3 | `index_status.papers_ready == false` (papers collection intentionally absent) | PASS |
| 4 | `index_status` active embedding is fastembed MiniLM (`provider=fastembed`, model contains `MiniLM`) | PASS |
| 5 | `search_notes("氧析出反应催化剂")` returns hits | PASS |
| 6 | `search_notes` ranks the OER note **#1** | PASS (`top source_file=oer_iridium_review_note.md`) |
| 7 | `get_note(source=...)` returns full note content (537 chars, contains 氧析出反应) | PASS |
| 8 | `search_papers(...)` degrades gracefully when papers collection is absent (structured `{"error": ...}`, process does not crash) | PASS |

Re-run command (after a prior `--build`):

```bash
LOCALRAG_HOME=<temp> LOCALRAG_NOTES_DIR=<temp> \
LOCALRAG_QUERY_LOG_ROOT=<temp>/_query_logs LOCALRAG_EMBED_PROVIDER=fastembed \
LOCALRAG_E2E_SPAWN_PYTHON=<a non-venv python> \
.venv/Scripts/python.exe scripts/verify_mcp_e2e.py --build
```

---

## Bugs found and fixed

### Bug 1 (blocking) — absent `papers` collection nils the notes collection

`service/query_server.py` startup created the ChromaDB `PersistentClient`
**inside** the papers-collection try-block. When `papers` did not exist
(a notes-only install: `build_notes_db.py` run but not `build_pdf_db.py`),
`client.get_collection("papers")` raised and the except handler set
`client = None`. The next block guarded the notes collection on
`if client`, so notes was silently disabled too — `search_notes` /
`get_note` returned *"Notes collection not initialized"* and
`index_status` reported `notes_ready=false`.

The first E2E run reproduced this exactly (checks 2, 5, 6, 7 FAILED;
`note_count=None`, `search_notes` payload `{"error": ...}`).

**Fix:** create the shared client first, independent of any single
collection; guard each collection fetch separately. Commit
`fix(service): keep ChromaDB client alive when papers collection is absent`.
Post-fix E2E: 8/8 PASS.

Impact: this is the exact "notes work with zero PDFs indexed" path a
fresh-clone user hits first, so it was a real release blocker for the MCP
retrieval promise.

### Bug 2 (cosmetic, provider-neutrality) — "Ollama OK" under fastembed

`service/build_notes_db.py` printed `"Ollama OK, embedding dim=..."` and,
on failure, `"[FATAL] Ollama not available"` — even under the default
`fastembed` provider, which uses in-process ONNX and no Ollama daemon. A
fresh-clone fastembed user would be told Ollama was involved, contradicting
the zero-daemon narrative.

**Fix:** report the configured provider + model
(`Embedding OK (fastembed:sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2), dim = 384`).
Commit `fix(service): provider-neutral embedding message in build_notes_db`.

---

## Claude Code / `.mcp.json` perspective

`.mcp.json` registers the server as `command: "python"`,
`args: ["scripts/run_mcp_server.py"]`. Claude Code was **not** launched for
this audit; instead the equivalent contract was validated directly: the E2E
spawn used a Python where the retrieval deps are *absent*, and the launcher
still produced a working server by re-exec'ing into `.venv`. That is the
same mechanism Claude Code relies on when it resolves bare `python`.

**Known limitation (Windows PATH).** `.mcp.json`'s `command: "python"`
depends on *some* `python` being resolvable on the host's PATH. On Windows,
if the user only ever installed Python via the Microsoft Store alias, or has
`py` but not `python`, the MCP host may fail to spawn the launcher at all
(before any re-exec can happen). The documented fallback is explicit
registration with an absolute interpreter, e.g.
`claude mcp add research-rag -- <abs python> scripts/run_mcp_server.py`,
or the HTTP sidecar (`service/query_server.py`). This is not fixed here;
it is inherent to a portable `command: "python"` entry.

---

## Known limitations (still outstanding after this audit)

- **Single machine, single OS.** This run is Windows 11 only, on the
  maintainer's box. No Linux/macOS execution and no clean-room fresh-OS
  bootstrap were performed. CI still runs only the unit/smoke suite.
- **Synthetic corpus.** 3 hand-written notes, not a real Zotero-derived
  vault. The `papers` collection (PDF chunks) was intentionally never built,
  so `search_papers` was exercised only on its graceful-degradation path,
  not on a populated collection.
- **fastembed only.** The `ollama` and `openai-compat` providers were not
  exercised end-to-end here.
- **fastembed mean-pooling notice.** fastembed 0.8.0 emits a UserWarning
  that `paraphrase-multilingual-MiniLM-L12-v2` now uses mean pooling instead
  of CLS embedding. Retrieval quality was fine for the discrimination test;
  noted for reproducibility if a future fastembed pin changes vectors.
