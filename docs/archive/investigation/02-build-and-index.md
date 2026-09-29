# 02 — Vector DB Build & Index Pipeline

_Investigation date: 2026-05-08 | Investigator: Claude Sonnet 4.6_

---

## 1. Data Flow Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         NOTES PIPELINE                                  │
│                                                                         │
│  $LOCALRAG_NOTES_DIR\*_review_note.md                                      │
│          │                                                              │
│          ▼                                                              │
│  parse_frontmatter()  →  (fm dict, body text)                          │
│          │                                                              │
│          ▼  [NO CHUNKING — whole document]                              │
│  get_embedding(full_text)  →  Ollama HTTP POST /api/embeddings          │
│          │                          model: qwen3-embedding:4b           │
│          │                          truncate at 12,000 chars            │
│          ▼                                                              │
│  col.upsert(id=MD5(filename), doc=full_text, emb=vector, meta=...)      │
│          │                                                              │
│          ▼                                                              │
│  ChromaDB collection "notes"  (cosine space)                           │
│  processed_notes.txt  ←  append filename on success                    │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                         PAPERS PIPELINE (research papers)               │
│                                                                         │
│  build_pdf_db.py reads $LOCALRAG_NOTES_DIR\*.md frontmatter               │
│          │  (pdf_0_path, pdf_1_path, … keys)                           │
│          ▼                                                              │
│  PDF file(s) per group  (main + optional SI files)                     │
│          │                                                              │
│          ▼                                                              │
│  extract_text_pdfplumber()  →  pdfplumber page-by-page join            │
│          │  truncate at last References/Bibliography/Acknowledgements   │
│          ▼                                                              │
│  chunk_text()  →  800-char chunks, 700-char step (100-char overlap)    │
│          │                                                              │
│          ▼  [OllamaEmbeddingFunction handles HTTP]                      │
│  col.add(docs, ids, metas)  ←  all chunks for one PDF at once          │
│          │                                                              │
│          ▼                                                              │
│  ChromaDB collection "papers"                                          │
│  processed_groups.txt  ←  append SHA-256 group hash on success         │
└─────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────┐
│                    PAPERS PIPELINE (large textbooks)                    │
│                                                                         │
│  CLI: ingest_textbook.py --pdf-path … --zotero-key …                   │
│          │  same extract → chunk logic as above                        │
│          │                                                              │
│          ▼  [BATCHED — 50 chunks per col.add() call]                   │
│  col.add(batch_50)  × N batches                                        │
│          │                                                              │
│          ▼                                                              │
│  ChromaDB collection "papers"  (same collection)                       │
│  textbook_ledger.txt  ←  append SHA-256 file hash on success           │
└─────────────────────────────────────────────────────────────────────────┘
```

**Key difference:** `build_notes_db.py` stores each note as **one document with no chunking** (`col.upsert` at line 156), while `build_pdf_db.py` and `ingest_textbook.py` split PDFs into 800-character chunks.

---

## 2. Two Collections Schema

### Collection: `notes`

- **Collection name:** `"notes"` (`build_notes_db.py` line 26)
- **Distance space:** cosine (`metadata={"hnsw:space": "cosine"}`, line 114)
- **ID format:** MD5 hex of the filename string (line 154: `hashlib.md5(filename.encode("utf-8")).hexdigest()`)
- **Document content:** complete note text including frontmatter (line 157: `documents=[text]`)
- **Write method:** `col.upsert` — re-running overwrites an existing record for the same filename

**Metadata fields** (lines 140–151):

| Key | Source | Always present? |
|-----|--------|-----------------|
| `source_file` | filename (e.g. `2024-NatCommun-…_review_note.md`) | Yes |
| `zotero_parent_key` | frontmatter `zotero_parent_key` | Yes (required; skips if missing) |
| `year` | frontmatter `year` | Optional |
| `journal` | frontmatter `journal` | Optional |
| `title_en` | frontmatter `title_en` | Optional |
| `title_zh` | frontmatter `title_zh` | Optional |
| `doi` | frontmatter `doi` | Optional |
| `authors` | frontmatter `authors` (list → joined string) | Optional |

Notes without `zotero_parent_key` are skipped but still appended to the ledger to avoid repeated attempts (line 133).

### Collection: `papers`

- **Collection name:** `"papers"` (`build_pdf_db.py` line 95, `ingest_textbook.py` line 22)
- **Distance space:** default (not explicitly set; ChromaDB default is L2)
- **ID format (research papers):** `"group_{group_idx}_file_{file_idx}_chunk_{k}"` (line 206)
- **ID format (textbooks):** `"textbook_{short_hash}_chunk_{k}"` where `short_hash = fhash[:16]` (line 130)
- **Document content:** raw text chunk, 800 chars max
- **Write method:** `col.add` (not upsert — duplicate IDs would error)

**Metadata fields** (lines 207–217 for research papers, lines 131–141 for textbooks):

| Key | Research Papers | Textbooks |
|-----|-----------------|-----------|
| `pdf_path` | full path string | full path string |
| `pdf_filename` | `os.path.basename(pdf_path)` | `os.path.basename(pdf_path)` |
| `paper_group` | group index (1-based) | `-1` (sentinel for textbook) |
| `file_index` | 0 = main, 1+ = SI | `0` always |
| `chunk_index` | chunk position within file | chunk position within file |
| `is_main` | `True` if `file_index == 0` | `True` always |
| `is_si` | `not is_main` | `False` always |
| `group_hash` | first 16 chars of SHA-256 group hash | first 16 chars of SHA-256 file hash |
| `zotero_parent_key` | queried from Zotero SQLite (or `""`) | CLI argument (required) |

---

## 3. Chunking Strategy for PDFs

**Parameters** (both scripts, identical values):
- `CHUNK_SIZE = 800` characters (`build_pdf_db.py` line 97)
- `CHUNK_STEP = 700` characters (`build_pdf_db.py` line 98)
- Overlap = `CHUNK_SIZE - CHUNK_STEP = 100` characters

**Boundary logic** (`chunk_text()`, lines 132–139):
```python
for i in range(0, len(text), CHUNK_STEP):
    chunk = text[i:i + CHUNK_SIZE]
    if len(chunk) > 100:   # discard very short tail fragments
        chunks.append(chunk)
```
Chunks are fixed-size character windows with no sentence or paragraph awareness. There is no page-boundary metadata stored per chunk.

**References truncation** (`extract_text_pdfplumber()`, lines 116–130):
The regex `REF_PATTERN` scans for the last occurrence of `References`, `REFERENCES`, `Bibliography`, `BIBLIOGRAPHY`, `参考文献`, `Acknowledgements`, or `ACKNOWLEDGEMENTS` followed by a newline. Everything from that heading onward is dropped before chunking. The scan uses `re.DOTALL | re.IGNORECASE`.

**SI vs main detection:** The first file in the group list (`file_idx == 0`) is `is_main=True`; all subsequent files are `is_si=True`. This is purely order-based — there is no filename pattern matching.

**Page metadata:** Not stored. `pdfplumber` pages are joined with `"\n"` before chunking; individual chunk records carry no page number.

---

## 4. Embedding Pipeline

### Notes pipeline (`build_notes_db.py`)

Uses a direct HTTP call via `urllib.request` (lines 48–61):

```python
req = urllib.request.Request(
    "http://localhost:11434/api/embeddings",
    data=json.dumps({"model": "qwen3-embedding:4b", "prompt": text}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
    method="POST",
)
with urllib.request.urlopen(req, timeout=60) as resp:
    return json.loads(resp.read())["embedding"]
```

- Text is truncated to `MAX_EMBED_CHARS = 12000` characters before the call (line 51)
- Timeout: 60 seconds
- No retry logic — any exception propagates and increments `failed` counter
- Called once per note document (one HTTP round-trip per note)

### Papers pipeline (`build_pdf_db.py`, `ingest_textbook.py`)

Uses `chromadb.utils.embedding_functions.OllamaEmbeddingFunction` (lines 151–154):

```python
ef = OllamaEmbeddingFunction(
    model_name="qwen3-embedding:4b",
    url="http://localhost:11434/api/embeddings"
)
```

The embedding function is registered with the collection via `get_or_create_collection(..., embedding_function=ef)`. ChromaDB calls it automatically when `col.add(documents=...)` is invoked. The internal batching, retry, and timeout behavior of `OllamaEmbeddingFunction` is opaque from the script — it is not customized.

**Batching:** In `build_pdf_db.py`, all chunks of one PDF are passed to `col.add()` in a single call (no explicit batching). In `ingest_textbook.py`, chunks are batched in groups of `BATCH_SIZE = 50` per `col.add()` call (lines 146–153).

---

## 5. Incremental Logic

### Notes ledger (`processed_notes.txt`)

- Format: one filename per line (e.g. `2024-NatCommun-…_review_note.md`)
- A note is considered processed if its filename appears in the ledger, regardless of file content
- **Implication:** updating a note's content does not trigger re-embedding unless the filename is manually removed from the ledger
- Notes missing `zotero_parent_key` are also written to the ledger (line 133) to suppress repeated skip messages
- Current ledger: 1104 entries

### Papers ledger (`processed_groups.txt`)

- Format: one SHA-256 hex string per line (64 chars)
- The hash covers all PDFs in a group concatenated in order (`get_combined_hash()`, lines 106–114): files are read in 8 KB blocks and hashed with SHA-256 in the order they appear in the group list (order is preserved, not sorted)
- A group is skipped if its combined hash is in the set
- **Re-embed trigger:** any file in the group changes (hash changes) → the group is reprocessed. But old chunks from the previous hash remain in ChromaDB (no delete before re-add)
- Ledger is appended only if `group_chunks_count > 0` (line 231)
- Current ledger: 1121 entries

### Textbook ledger (`textbook_ledger.txt`)

- Format: one SHA-256 hex string per line (individual file hash)
- Current ledger: 2 entries (two textbooks ingested)
- The ledger key is the single-file SHA-256 (`file_hash(path)`, lines 34–39)

### Difference from textbook path

`ingest_textbook.py` is CLI-driven (explicit `--pdf-path` and `--zotero-key` arguments), while `build_pdf_db.py` auto-discovers all PDFs by scanning note frontmatter. Textbook ingest also batches writes in groups of 50 chunks to avoid timeout on large books.

---

## 6. Stub Note Workflow

The stub-note mechanism is the **discovery trigger** for `build_pdf_db.py`. Instead of hard-coding PDF paths, the script reads every `.md` file in `$LOCALRAG_NOTES_DIR` and looks for frontmatter keys `pdf_0_path`, `pdf_1_path`, etc. (`extract_pdf_groups_from_notes()`, lines 59–88).

A **stub note** is a Markdown file that may contain only a YAML frontmatter block (with no review body yet) but includes `pdf_0_path` pointing to the actual PDF. When a new paper is added to Zotero, the gemini-literature-processor SKILL generates a review note that always includes these `pdf_N_path` keys in the frontmatter. Running `build_pdf_db.py` then picks up the new note automatically.

Concrete flow:
1. New paper PDF lands in the Zotero storage directory (e.g., `$ZOTERO_ATTACHMENT_BASE_DIR\…pdf`)
2. `gemini-literature-processor` generates `$LOCALRAG_NOTES_DIR\<year>-<journal>-…_review_note.md` with frontmatter containing:
   ```yaml
   pdf_0_path: "E:\...\paper.pdf"
   pdf_1_path: "E:\...\paper_SI.pdf"   # if SI exists
   ```
3. `build_pdf_db.py` runs (script-level code at lines 90–92 executes `extract_pdf_groups_from_notes()` at import time), finds the new note, computes the group hash, sees it is not in `processed_groups.txt`, extracts text, chunks, embeds, and writes to ChromaDB
4. The group hash is appended to `processed_groups.txt`

If `os.path.exists(path)` returns False for a declared PDF path (line 78), the path is skipped with a `[WARN]` message and the group is omitted (no group is created for a note with zero valid PDF paths).

---

## 7. Textbook Special Path

`ingest_textbook.py` differs from `build_pdf_db.py` in the following ways:

| Aspect | `build_pdf_db.py` | `ingest_textbook.py` |
|--------|-------------------|-----------------------|
| Discovery | Automatic (scans note frontmatter) | Manual (CLI `--pdf-path`) |
| Zotero key | Auto-queried via SQLite | Required CLI argument |
| Groups | Multi-file (main + SI) | Single file only |
| `col.add()` calls | One per PDF (all chunks at once) | Batches of 50 chunks |
| Ledger key | SHA-256 of all files in group | SHA-256 of single file |
| `paper_group` sentinel | 1-based group index | `-1` |
| `--dry-run` flag | Not available | Available (lines 113–116) |
| Target file size | 20–80 pages (per header comment) | 200+ pages |

Both use the same `CHUNK_SIZE=800`, `CHUNK_STEP=700`, same `REF_PATTERN`, same `pdfplumber` extraction, same `"papers"` collection, and same `OllamaEmbeddingFunction`.

---

## 8. Hard-Coded Paths and Constants

### Absolute paths

| Constant | Value | File | Line |
|----------|-------|------|------|
| `ZOTERO_DB` | `$ZOTERO_DB_PATH` | `build_pdf_db.py` | 27 |
| `NOTES_DIR` | `$LOCALRAG_NOTES_DIR` | `build_pdf_db.py`, `build_notes_db.py` | 57, 23 |
| `CHROMA_PATH` | `$LOCALRAG_HOME\chroma` | all three scripts | 94, 24, 21 |
| `LEDGER_PATH` (papers) | `$LOCALRAG_HOME\processed_groups.txt` | `build_pdf_db.py` | 96 |
| `LEDGER_PATH` (notes) | `$LOCALRAG_HOME\processed_notes.txt` | `build_notes_db.py` | 25 |
| `LEDGER_PATH` (textbook) | `$LOCALRAG_HOME\textbook_ledger.txt` | `ingest_textbook.py` | 23 |

### URLs

| Constant | Value | File | Line |
|----------|-------|------|------|
| `OLLAMA_URL` | `http://localhost:11434/api/embeddings` | `build_notes_db.py` | 27 |
| Ollama URL (inline) | `http://localhost:11434/api/embeddings` | `build_pdf_db.py` | 153 |
| Ollama URL (inline) | `http://localhost:11434/api/embeddings` | `ingest_textbook.py` | 121 |

### Model names

| Constant | Value | File | Line |
|----------|-------|------|------|
| `EMBED_MODEL` | `"qwen3-embedding:4b"` | `build_notes_db.py` | 28 |
| `model_name` | `"qwen3-embedding:4b"` | `build_pdf_db.py` | 152 |
| `model_name` | `"qwen3-embedding:4b"` | `ingest_textbook.py` | 120 |

### Collection names

| Constant | Value | File | Line |
|----------|-------|------|------|
| `COLLECTION_NAME` | `"notes"` | `build_notes_db.py` | 26 |
| `COLLECTION_NAME` | `"papers"` | `build_pdf_db.py` | 95 |
| `COLLECTION` | `"papers"` | `ingest_textbook.py` | 22 |

### Thresholds and windows

| Constant | Value | Purpose | File | Line |
|----------|-------|---------|------|------|
| `CHUNK_SIZE` | `800` chars | max chunk length | `build_pdf_db.py`, `ingest_textbook.py` | 97, 24 |
| `CHUNK_STEP` | `700` chars | step between chunks | `build_pdf_db.py`, `ingest_textbook.py` | 98, 25 |
| `BATCH_SIZE` | `50` chunks | textbook write batch | `ingest_textbook.py` | 26 |
| `MAX_EMBED_CHARS` | `12000` chars | notes embedding truncation | `build_notes_db.py` | 32 |
| min chunk length | `100` chars | discard tiny tail chunks | all scripts | `build_pdf_db.py:138` |
| Ollama timeout | `60` seconds | notes HTTP call | `build_notes_db.py` | 60 |

---

## 9. Failure Modes & Quirks

### If Ollama is down

- **Notes pipeline:** `get_embedding("test")` is called as a pre-flight check (line 87). On failure, the script prints `[FATAL] Ollama not available: …` and exits with code 1 (`sys.exit(1)`). No notes are processed.
- **Papers pipeline:** No pre-flight check. `OllamaEmbeddingFunction` is passed to ChromaDB; the failure surfaces when `col.add()` is first called, wrapped in a generic `except Exception as e` block (line 228) that prints `[ERROR]` and continues to the next PDF. The group hash is NOT written to the ledger (only written if `group_chunks_count > 0`), so the group will be retried on the next run.
- **Textbook pipeline:** No pre-flight check. `col.add()` failure would propagate as an unhandled exception and abort the script.

### If a PDF fails to extract text

- `extract_text_pdfplumber()` raises an exception → caught by the `except Exception` at line 228 (`build_pdf_db.py`)
- If all files in a group fail, `group_chunks_count` stays 0 → ledger not written → group retried on next run
- Scanned (image-only) PDFs yield empty text: detected at line 194 (`if not full_text.strip()`) and logged as `[WARNING]`, continuing to next file

### Duplicate hashes / re-runs

- **Papers:** If `col.add()` is called with an ID already in ChromaDB, ChromaDB raises a `DuplicateIDError` (not caught by the script). This could happen if the ledger is manually cleared and the script is re-run against the same unchanged PDFs. The IDs are deterministic (`group_N_file_M_chunk_K`), so the collision is guaranteed.
- **Notes:** Uses `col.upsert()` — safe for re-runs on the same filename.
- **Stale chunks:** When a PDF group changes (new hash), the old chunks remain in ChromaDB under the old IDs. There is no cleanup step before adding new chunks. Over time this could produce duplicate semantic content from different versions of the same paper.

### Notes ledger vs. content hash

The notes ledger tracks filenames, not content hashes. Editing a note's body does not cause re-embedding unless the filename is removed from `processed_notes.txt`.

---

## 10. Open Questions / Surprises

### `wave8_gold_ledger.txt` purpose

This is **not a ChromaDB build ledger**. It is a JSON metadata file (not a list of hashes) describing a separate mini-database called `wave8_gold_claims_v1` stored at `$LOCALRAG_HOME\chroma_wave8_gold` (a different ChromaDB path from the main `chroma` directory). It records 841 rows ingested from `ai_preextract_template.csv` files from a `wave8/gold_annotations/` annotation project for a CO2RR Raman study. The file has 15 papers listed; `G4_UWX96PKF` is flagged `is_blocked=True`. This collection is entirely separate from the `notes`/`papers` pipeline and is not built by any of the three scripts under investigation.

### Multiple `check_db*.py` versions

All four files (`check_db.py` through `check_db4.py`) target a **different database** entirely: `$HOME\.openclaw\memory\main.sqlite` — a SQLite FTS database, not ChromaDB. They are diagnostic scripts for the `.openclaw` memory system (separate from `.localrag`). They have no relationship to the ChromaDB `notes` or `papers` collections. Their presence in the `.localrag` directory appears to be an organizational accident.

### `test_build.py` uses `docling` instead of `pdfplumber`

The test script (`test_build.py`) uses `from docling.document_converter import DocumentConverter` (line 3) and calls `result.document.export_to_markdown()`, which is a fundamentally different extraction path from the production scripts (`pdfplumber`). It also uses a hardcoded `PDF_GROUPS` list rather than frontmatter discovery, and writes to a separate `"papers_test"` collection. It pre-deletes the collection on each run. This was clearly an early prototype.

### ChromaDB persistence layout

The `chroma` directory contains exactly two UUID-named subdirectories plus `chroma.sqlite3`. Based on size:
- `fb1195f1-…` (833 MB `data_level0.bin`, 321 KB `length.bin`) is the `papers` collection (large, many chunks)
- `cf0706e7-…` (10 MB `data_level0.bin`, 4 KB `length.bin`) is the `notes` collection (794 whole-document records)

### `OllamaEmbeddingFunction` vs. direct HTTP

The notes pipeline calls Ollama directly via `urllib.request` while the papers pipeline delegates to ChromaDB's `OllamaEmbeddingFunction`. This means error handling, retry behavior, and connection pooling differ between the two pipelines. The notes pipeline has a 60-second explicit timeout; the papers pipeline has whatever the embedding function's default is (not visible without reading the ChromaDB library source).

### No page-level metadata in `papers` chunks

Individual chunks have no `page_number` field. Retrieval results from `papers` cannot be mapped back to a specific page. This may matter for citation generation in the query layer.

### `processed_groups.txt` has 1121 entries vs. `processed_notes.txt` has 1104 entries

There are more PDF groups processed than notes indexed. This is consistent with: (a) some notes missing `zotero_parent_key` are skipped by the notes pipeline but still counted in the notes ledger, and (b) the counts reflect different ledger formats (hash vs. filename). The 17-entry gap is not alarming but worth monitoring.
