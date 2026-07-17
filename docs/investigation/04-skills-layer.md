# Skills Layer Investigation

*Generated 2026-05-08 | Scope: Claude Code skills that orchestrate the local literature retrieval system*

---

## 1. Skill Inventory

| Skill | Directory | Purpose | Supporting Files |
|-------|-----------|---------|-----------------|
| `search-literature` | `$HOME\.claude\skills\search-literature\` | Unified retrieval entry point; dispatches WF1a–WF10 | SKILL.md only |
| `search-notes` | `$HOME\.claude\skills\search-notes\` | Leaf: queries ChromaDB `notes` collection | SKILL.md only |
| `search-papers` | `$HOME\.claude\skills\search-papers\` | Leaf: queries ChromaDB `papers` collection (PDF chunks) | SKILL.md only |
| `gemini-literature-processor` | `skills/gemini-literature-processor\` (canonical) | Note generation: Zotero → Gemini → structured notes → vault | scripts/, prompts/, schemas/, template_rules/, references/, processed_history.txt |
| `literature-tagging-pipeline` | `$HOME\.claude\skills\literature-tagging-pipeline\` | Post-generation tagging: Kimi batch tagging of `*_review_note.md` | scripts/ (watch_tagging_pipeline.ps1) |
| `rag-engineer` | `$HOME\.claude\skills\rag-engineer\` | Meta: design principles for RAG pipelines over this corpus | SKILL.md only (+ rag-engineer.zip) |
| `vector-database-engineer` | `$HOME\.claude\skills\vector-database-engineer\` | Meta: ChromaDB collection design and rebuild strategy | SKILL.md only (+ zip) |
| `embedding-strategies` | `$HOME\.claude\skills\embedding-strategies\` | Meta: embedding model selection and benchmarking | SKILL.md only (+ zip) |
| `electrocatalysis-paper-audit` | `$HOME\.claude\skills\electrocatalysis-paper-audit\` | Domain: structured audit of electrocat manuscripts (draft/revision/pre-submission) | references/ (advisor-style-whitelist-blacklist.md), examples/ (etp-fuel-cell-pre-submission-example.md), examples.zip |
| `experiment-record-digitizer` | `$HOME\.claude\skills\experiment-record-digitizer\` | Adjacent: OCR handwritten lab notes to structured Markdown | SKILL.md only |

---

## 2. Entry-Point UX

### Trigger mapping

| User intent | Skill triggered | Slash command | Endpoint family |
|-------------|----------------|---------------|-----------------|
| "有哪些论文研究了X" / "查一下X" | `search-literature` | `/search-literature` | `POST /search_notes` |
| "原文怎么说" / "验证一下" | `search-literature` → WF3 | `/search-literature` | `POST /search_papers` |
| "详细分析这篇" | `search-literature` → WF7 | `/search-literature` | `/search_notes` + `/get_note` + `/search_papers` |
| "研究怎么发展的" | `search-literature` → WF8 | `/search-literature` | `POST /search_notes` |
| "有没有争议" | `search-literature` → WF10 | `/search-literature` | `POST /search_notes` |
| Direct notes query | `search-notes` | `/search-notes` | `POST /search_notes` (port 18800) |
| Direct PDF query | `search-papers` | `/search-papers` | `POST /search_papers` (port 18800) |
| "处理新增论文" / "批量生成笔记" | `gemini-literature-processor` | `/gemini-literature-processor` | `zotero_batch_scanner.py` |
| Tag review notes with Kimi | `literature-tagging-pipeline` | `/literature-tagging-pipeline` | `run_tagging_pipeline.ps1` |
| Audit electrocat manuscript | `electrocatalysis-paper-audit` | `/electrocatalysis-paper-audit` | None (pure LLM analysis) |

**Port discrepancy to resolve before packaging**: `search-literature` declares `http://127.0.0.1:18810` throughout; `search-notes` and `search-papers` each declare `http://127.0.0.1:18800`. One of these is stale. Canonical port needs to be locked to a single value in all three skill files.

---

## 3. search-literature WF Protocol

This is the headline design pattern for the open-source repo. It is a five-step state machine executed by the LLM against two ChromaDB endpoints.

### Step 0 — Angle Planning (mandatory before any query)

Every retrieval begins with two-level query decomposition:

- **Anchor angle** (mandatory): faithful preservation of the user's intent, translated to English if needed. This is never replaced by exploratory angles. It is the semantic anchor that prevents search drift.
- **Exploratory angle(s)** (at least 1): chosen from a typed vocabulary — `core_concept`, `mechanism`, `evidence`, `contrast`, `method`, `timeline`, `paper_specific`. The type determines which WF is paired with it.

Default probe: anchor + 1 exploratory angle → fire → assess coverage → expand to 3–5 angles only if results are scattered, evidence is missing, or contradictions surface. The LLM is explicitly prohibited from treating synonym substitution as a multi-angle search.

### Step 1 — Intent Recognition + User Confirmation

The LLM internally classifies the request on six dimensions (depth, range, data source, special needs, initial angles, expansion conditions) before speaking. It then surfaces a mandatory confirmation message:

> "我打算用 WF{N}·{名称} 来处理这个问题。会先从 2 个角度试探检索…要继续吗？"

This is a hard UX contract: no query fires without the user seeing which WF was chosen and why. If the user declines, the full WF menu (WF1a through WF10) is presented for selection. Two pre-conditions require asking first: unknown paper identity, and ambiguity between WF1a vs WF1b.

### Step 2 — WF Selection

Ten named workflows with explicit endpoint call signatures:

| WF | Name | Core call | Key parameter |
|----|------|-----------|---------------|
| WF1a | 快速多篇检索 | `POST /search_notes` | `dedupe: true, n: 5` |
| WF1b | 单篇多角度检索 | `POST /search_notes` | `zotero_parent_key`, `dedupe: false` |
| WF2 | 指定论文笔记 | `POST /search_notes` | `zotero_parent_key, n: 3` |
| WF3 | 纯原文检索 | `POST /search_papers` | English query mandatory |
| WF4 | 笔记→原文联动 | `/search_notes` → `/search_papers` | `second_query` from note's existing English terms |
| WF5 | 原文→笔记反向 | `/search_papers` → `/search_notes` | key extracted from paper result |
| WF6 | 横向对比多篇 | `POST /search_notes` | `n: 10, dedupe: true`, grouped by year/journal |
| WF7 | 完整精读单篇 | `/search_notes` → `/get_note` → `/search_papers` | `summary_only: false` |
| WF8 | 时间线检索 | `POST /search_notes` | `n: 10`, results sorted ascending by `metadata.year` |
| WF9 | 实验方法检索 | `POST /search_papers` first | prioritize `is_si: true` results |
| WF10 | 矛盾检测 | `POST /search_notes` | `n: 10, dedupe: true`, LLM identifies contradictions |

WF4 is the most powerful pattern: it uses a two-pass strategy where the `second_query` parameter is populated with English terms already present in the note (not LLM translation), ensuring precise cross-lingual retrieval. WF2.5 maps WF number to the angle type that should be paired with it.

### Step 3 — Output Format

Fixed output template per result type: notes use `📓` prefix with filename, rank, and similarity score; papers use `📄` prefix with filename, main/SI flag, original English passage, and Chinese translation. Every retrieval session ends with a mandatory footer offering context expansion, Feishu save, and deep-dive options. Context expansion fires a second call with `include_context: true`, returning `[MATCH]...[/MATCH]`-bracketed passages displayed bold.

Local research reports go to `$LOCALRAG_NOTES_DIR\reports\` using `YYYY-MM-DD-{topic-slug}.md` naming. Query logs go to `_query_logs\`. Process artifacts go to `progress\`.

### Step 4 — Feishu Save

On user reply "保存", fires `POST /write_to_feishu` with workflow ID, original query, notes results array, and papers results array. If the week's document is not registered, user is prompted to provide the Feishu docx URL.

### Step 5 — Query Log

After every main WF completion, fires `POST /write_query_log` with a mandatory `idempotency_key` (stable within a research session), `anchor_query`, `planned_angles`, `executed_angles`, `expansion_reason`, `stop_reason`, and `search_runs` array. Follow-up actions (context expand, Feishu save, deeper dive) append to the existing log via `POST /append_query_log_action` — they never create a new log file. One user research question = one log file with all its follow-ups accumulated inside.

---

## 4. search-notes and search-papers Leaf Skills

Both are independently addressable via `/search-notes` and `/search-papers` but are designed to be called by `search-literature`, not independently by the user in normal flow.

**What they add beyond search-literature's coverage:**

- `search-notes`: Explicitly documents the `get_note` endpoint parameter schema (`summary_only: true/false`), the data source path (`$LOCALRAG_NOTES_DIR\*_review_note.md`), the build script (`build_notes_db.py`), and the ledger file (`processed_notes.txt`). It also clarifies that the notes collection stores whole notes (no chunking) so the `dedupe` flag is semantically different from what it would mean in a chunked store. The error-handling section details the Ollama startup sequence and port-occupation diagnostic.
- `search-papers`: Documents `second_query` parameter semantics (for WF4), the `content` vs `content_original` response fields (showing that the server pre-concatenates neighboring chunks into ~2400-char windows), and the `is_main`/`is_si` flags. It also documents `ingest_textbook.py` as the large-PDF alternative to `build_pdf_db.py` (≥200 pages, batched 50 chunks to avoid ChromaDB timeout). The Feishu write endpoint is also documented here with its `document_id` parameter.

Neither leaf skill is redundant — they serve as the operational reference for the query_server.py API that search-literature only describes at call-site level.

**Discrepancy**: `search-notes` and `search-papers` both declare port `18800`; `search-literature` declares port `18810`. This is a real inconsistency that must be resolved before open-source packaging.

---

## 5. gemini-literature-processor Protocol

The write/ingest half of the system. All reading goes through search-literature; all note generation goes through this skill.

### Pre-flight (always required)

Before any command executes:
1. Confirm Zotero is closed (SQLite database lock).
2. Confirm scope (A/B/C/D).

### Python and env lock

Hardcoded at the top of every session:
- `$PYTHON311 = "$LOCALRAG_MAIN_PYTHON"` — for Zotero scanning, Gemini API, GCS
- `$PYTHON_RAG = "$LOCALRAG_RAG_PYTHON"` — for ChromaDB rebuild post-ingestion
- GCP credentials, project, location, and GCS bucket set via `$env:` variables

### Four invocation modes

| Mode | Command | When |
|------|---------|------|
| A — full scan | `zotero_batch_scanner.py` (no `--since` or `--limit`) | Process all unprocessed papers |
| B — limited test | `zotero_batch_scanner.py --limit N` | Validation run before full batch |
| C — recent additions | `zotero_batch_scanner.py --since YYYY-MM-DD` | Incremental after new imports |
| D — single PDF | `gemini_analyze_pdf.py "path/to/paper.pdf"` | Only when user explicitly provides path |

**Why `--since` + `--limit` are mandatory together for "recent" queries**: `--since` alone can return an unbounded set if many papers were imported on the given date, leading to uncontrolled API costs and timeouts. The skill protocol routes mode C through `--since` only, but for mode B (`--limit N`) it is implied that users verify with a small N before going broad. The skill text does not explicitly combine `--since` and `--limit` in the same command, but the protocol's design intent is that B is always used to validate before A or C.

### Canary output before live-vault promotion

Notes first land in `$LOCALRAG_NOTES_DIR\progress\pipeline_reports\gemini_incremental_alignment\canary_notes\`. Live-vault promotion requires passing deterministic prefill and frontmatter validation. Candidate-first note rules: `tags: []` empty, `candidate_tags_high/medium/low` filled, `human_reviewed: 0`.

### Auto-ingest (Step 3 of post-generation)

After vault promotion: start Ollama, run `build_notes_db.py`, run `build_pdf_db.py`, restart `query_server.py`. This rebuilds both ChromaDB collections and makes new notes immediately searchable.

### Report

Always output: processed count, skipped count, total note count, total PDF chunk count, failed items with reason.

---

## 6. Meta-Skill Role

`rag-engineer`, `vector-database-engineer`, and `embedding-strategies` are opinionated engineering guidelines, not workflow orchestrators.

**They describe THIS system specifically**: all three embed the exact local paths (`$LOCALRAG_RAG_PYTHON`, `$LOCALRAG_HOME\chroma`, `qwen3-embedding:4b`), the ChromaDB version target (1.5.5), and the two-collection design (`notes` + `papers`). They are not generic templates repurposed here — they were written for this stack.

**Cross-references**: None of these three skills are referenced from `search-literature` or `gemini-literature-processor` SKILL.md files. They are stand-alone advisory skills invoked when the user is debugging retrieval quality, designing a new collection, or evaluating whether to replace the embedding model. They operate at the infrastructure level, below the WF protocol.

**Packaging implication**: ~~Ship them in a `skills/infra/` or `skills/meta/` subfolder rather than alongside the user-facing retrieval skills.~~ Updated 2026-05-09: Anthropic's plugin layout requires a flat `skills/<name>/` namespace, so these moved to `skills/rag-engineer/`, `skills/vector-database-engineer/`, `skills/embedding-strategies/` alongside the user-facing retrieval skills. They are useful reference for contributors, not for end users.

---

## 7. literature-tagging-pipeline

This is a post-generation step separate from `gemini-literature-processor`. The pipeline processes notes that already exist in `$LOCALRAG_NOTES_DIR` and applies structured tags through Kimi batch execution.

**Relationship to gemini-literature-processor**: gemini-literature-processor generates notes with `tags: []` (empty) and `candidate_tags_high/medium/low` (machine candidates). literature-tagging-pipeline then runs Kimi to populate the final tags and manages the review lifecycle. The two skills are sequential: generate → tag → (human review).

**State files owned**:
- `$LOCALRAG_NOTES_DIR\progress\tagging_state.jsonl` — per-note tagging state, the authoritative record
- `$LOCALRAG_NOTES_DIR\status\current-status.md` — current pipeline state
- `$LOCALRAG_NOTES_DIR\status\task-log.md` — append-only task history
- `$LOCALRAG_NOTES_DIR\wiki\taxonomy.yaml`, `tag_aliases.yaml`, `material_hierarchy.yaml` — tag ontology
- `$LOCALRAG_NOTES_DIR\wiki\tagging_prompt.md` — Kimi prompt
- Gate reports: `$LOCALRAG_NOTES_DIR\progress\gate_reports\`
- Pipeline reports: `$LOCALRAG_NOTES_DIR\progress\pipeline_reports\`

**Watcher script**: `skills/literature-tagging-pipeline/scripts/watch_tagging_pipeline.ps1` — monitors a running pipeline session without touching vault files.

The skill enforces hard safety rules: no parallel pipeline runs against the same vault, no manual edits to `progress/*` to fake gate results, new tag concepts must be flagged as `CANDIDATE_NEEDED` rather than directly appended to the taxonomy.

---

## 8. Skill Duplication and Drift

| Skill | Canonical location | Known mirrors | Status |
|-------|-------------------|---------------|--------|
| `gemini-literature-processor` | `skills/gemini-literature-processor\` | `.openclaw\` (confirmed same content), `.cc-switch\` (exists per SKILL.md reference), `.codex\` (directory present, not `.codex\skills\`) | `.agents\` is canonical; mirrors confirmed identical at time of read |
| `search-literature` | `$HOME\.claude\skills\search-literature\` | None found | Single source |
| `search-notes` | `$HOME\.claude\skills\search-notes\` | None found | Single source |
| `search-papers` | `$HOME\.claude\skills\search-papers\` | None found | Single source |
| `literature-tagging-pipeline` | `$HOME\.claude\skills\literature-tagging-pipeline\` | Scripts in `.agents\skills\literature-tagging-pipeline\` | SKILL.md lives in `.claude\`; watcher script lives in `.agents\` |

The `.openclaw\` `processed_history.txt` has its own backup series alongside the `.agents\` version. The canonical ledger is `skills/gemini-literature-processor\processed_history.txt` — the `.openclaw\` copy appears to be a symlink target or was manually synced. The pre-unify backup (`processed_history.txt.pre_unify_20260326_231129.openclaw.bak`) confirms there was a merge event in March 2026 that unified the two histories.

**Packaging decision**: ship only the `.agents\` version for `gemini-literature-processor`. The `.openclaw\` and `.cc-switch\` mirrors are compatibility shims for other Claude agent runners; do not include them in the open-source repo unless those runners are also being shipped.

---

## 9. Hard-Coded Paths and Assumptions

### Absolute paths (Windows-specific)

| Path | Which skills | Role |
|------|-------------|------|
| `$LOCALRAG_MAIN_PYTHON` | gemini-literature-processor, rag-engineer, embedding-strategies, literature-tagging-pipeline | Main Python for Gemini/GCS/Zotero |
| `$LOCALRAG_RAG_PYTHON` | all search skills, gemini-literature-processor, rag-engineer, vector-database-engineer | ChromaDB/LocalRAG Python |
| `$LOCALRAG_HOME\query_server.py` | search-literature, search-notes, search-papers | Query server |
| `$LOCALRAG_HOME\chroma` | rag-engineer, vector-database-engineer | ChromaDB data root |
| `$LOCALRAG_HOME\processed_groups.txt` | rag-engineer | PDF ingestion ledger |
| `$LOCALRAG_HOME\build_notes_db.py`, `build_pdf_db.py` | search-notes, search-papers, gemini-literature-processor | DB build scripts |
| `skills/gemini-literature-processor\processed_history.txt` | gemini-literature-processor | Note generation dedup ledger |
| `scanner/*.py` | gemini-literature-processor | Scanner and analysis scripts |
| `$HOME\<your-service-account>.json` | gemini-literature-processor | GCP service account credential |
| `ollama` | search-literature, search-notes, search-papers, gemini-literature-processor | Ollama binary |
| `$ZOTERO_DB_PATH` | gemini-literature-processor, rag-engineer | Zotero database |
| `$ZOTERO_ATTACHMENT_BASE_DIR` | gemini-literature-processor | Zotero file storage base |
| `$LOCALRAG_NOTES_DIR` | all skills | Notes vault root |
| `$LOCALRAG_NOTES_DIR\reports\` | search-literature | Compiled research reports |
| `$LOCALRAG_NOTES_DIR\progress\tagging_state.jsonl` | literature-tagging-pipeline | Tagging state |
| `$LOCALRAG_NOTES_DIR\scripts\run_tagging_pipeline.ps1` | literature-tagging-pipeline | Pipeline runner |
| (separate vault, not in this repo) | experiment-record-digitizer | Lab note archive — out of scope here |

### Service endpoints

- Query server: `http://127.0.0.1:18810` (search-literature) vs `http://127.0.0.1:18800` (search-notes, search-papers) — **unresolved conflict**
- Ollama embedding: `http://localhost:11434/api/embeddings`
- Ollama health: `http://localhost:11434/api/tags`

### Language assumptions

- All search queries to `search-papers`/`/search_papers` must be English (Chinese input triggers mandatory translation step in WF3/WF9). The `second_query` in WF4 must be English extracted from existing note text, not LLM-translated.
- Output always includes Chinese translation of English paper excerpts.
- User-facing prompts in `gemini-literature-processor` are bilingual but default to Chinese.
- `search-literature` description field is Chinese; search-notes and search-papers descriptions are mixed.

### Windows-specific assumptions

- All startup scripts use PowerShell (`Start-Process`, `Invoke-RestMethod`, `Get-Process`, `Where-Object`, `Stop-Process`).
- Path separators throughout are backslash.
- Paths span three drive letters: C: (system/tools), D: (Zotero), E: (university files), F: (research notes).
- No Unix equivalents are documented.

---

## 10. Plugin Packaging Implications

### Recommended folder layout

```
skills/
├── search-literature/
│   └── SKILL.md
├── search-notes/
│   └── SKILL.md
├── search-papers/
│   └── SKILL.md
├── gemini-literature-processor/
│   ├── SKILL.md
│   ├── scripts/              (7 Python scripts)
│   ├── prompts/              (3 system prompt .txt files)
│   ├── schemas/              (3 Vertex JSON schemas)
│   ├── template_rules/       (7 domain-specific rule files)
│   └── references/           (3 runbook .md files)
├── literature-tagging-pipeline/
│   ├── SKILL.md
│   └── scripts/              (watch_tagging_pipeline.ps1)
└── infra/
    ├── rag-engineer/
    │   └── SKILL.md
    ├── vector-database-engineer/
    │   └── SKILL.md
    └── embedding-strategies/
        └── SKILL.md
```

Domain skills (`electrocatalysis-paper-audit`, `experiment-record-digitizer`) are not part of the retrieval system proper and should ship in a separate `domain-skills/` folder or be excluded from the core package.

### Dependency graph

```
search-literature
  ├── depends on: search-notes (endpoint /search_notes)
  ├── depends on: search-papers (endpoint /search_papers)
  └── depends on: query_server.py (runtime, not a skill)

gemini-literature-processor
  └── feeds into: search-literature (generates the notes that search-literature retrieves)
      └── triggers: literature-tagging-pipeline (post-generation tagging step)

infra skills (rag-engineer, vector-database-engineer, embedding-strategies)
  └── stand-alone; no runtime dependencies between them and the search skills
```

### Pre-packaging fixes required

1. **Resolve port conflict**: standardize all three search skills to one port (18810 or 18800).
2. **Parameterize absolute paths**: extract `$HOME\...`, `D:\...`, `E:\...`, `F:\...` into a config file or environment variables section at the top of each SKILL.md. This is the single biggest barrier to portability.
3. **Strip personal credential reference**: `<your-service-account>.json` path must become `$env:GOOGLE_APPLICATION_CREDENTIALS` with no default value.
4. **Document minimum runtime dependencies**: Ollama + qwen3-embedding:4b, ChromaDB 1.5.5 Python venv, query_server.py, Zotero (for gemini-literature-processor only).
5. **Clarify `--since` + `--limit` interaction** in gemini-literature-processor Mode C documentation to prevent unbounded API cost for new users.
