---
name: rag-engineer
description: "Use when designing, debugging, or improving retrieval-augmented generation over PDFs, Zotero libraries, notes, or local research corpora, especially when chunking, metadata, recall, or evaluation are the bottleneck."
---

# RAG Engineer

Design and repair retrieval systems before worrying about prompt polish. In this environment, retrieval quality is usually the limiting factor.

## Local environment

- Main Python: `$LOCALRAG_MAIN_PYTHON`
- LocalRAG Python: `$LOCALRAG_RAG_PYTHON`
- Chroma data root: `$LOCALRAG_HOME\chroma`
- PDF group ledger: `$LOCALRAG_HOME\processed_groups.txt`
- Zotero database: `$ZOTERO_DB_PATH`
- Research note output: `$LOCALRAG_NOTES_DIR`
- Default embedding provider: in-process `fastembed`
- Optional Ollama tier: `qwen3-embedding:0.6b` by default; `4b` is an upgrade for larger-memory hosts

## Use this skill when

- A retrieval pipeline misses obviously relevant documents.
- The answer quality is fine once the right chunks are found, but bad otherwise.
- You need to redesign chunking, metadata, filtering, reranking, or evaluation.
- A PDF ingestion pipeline is slow, duplicated, or hard to refresh safely.
- You are bridging PDFs, Zotero metadata, notes, and a vector store into one workflow.

## Default workflow

1. Define the retrieval task precisely.
   - What should be returned: document, section, paragraph, note, or citation?
   - What are the real queries: keyword-heavy, concept-heavy, bilingual, or citation lookup?
2. Inspect corpus boundaries before indexing.
   - Distinguish source PDFs, generated notes, and derived summaries.
   - Avoid mixing raw source truth with synthesized notes unless metadata makes the origin explicit.
3. Design metadata first.
   - Keep stable identifiers for `zotero_key`, source file path, section title, language, and ingestion timestamp.
   - Add corpus-level tags so filters can narrow the search space before vector lookup.
4. Choose chunking based on meaning, not arbitrary size.
   - Preserve headings, figure/table context, numbered steps, and citation neighborhoods.
   - Use overlap only where it preserves context; do not hide bad segmentation behind huge overlap.
5. Evaluate retrieval separately from generation.
   - Save test queries and expected hits.
   - Measure top-k recall, ranking quality, and false-positive patterns before touching the LLM prompt.
6. Rebuild safely.
   - Use the LocalRAG Python for Chroma and vector-store operations.
   - Use the main Python for generic scripts, API clients, Playwright, Gemini, and preprocessing outside Chroma.

## Good patterns

- Hierarchical retrieval:
  first retrieve sections or documents, then resolve to smaller chunks.
- Metadata-first narrowing:
  filter by corpus, note type, or Zotero parent before similarity search.
- Dual-source pipelines:
  keep raw PDF chunks and note chunks in distinct collections or with explicit source metadata.
- Evaluation sets:
  maintain a fixed benchmark query list so tuning has a stable target.

## Bad patterns

- One giant mixed collection with no provenance fields.
- Fixed chunk sizes that split equations, captions, or method steps mid-thought.
- Treating note summaries as equal to source evidence.
- Tweaking prompts before measuring retrieval failures.
- Editing `processed_groups.txt` casually without understanding ingestion state.

## Environment guardrails

- Use `$LOCALRAG_RAG_PYTHON` for ChromaDB and LocalRAG operations.
- Use `$LOCALRAG_MAIN_PYTHON` for everything else.
- Prefer primary-source validation for literature questions. If the user asks about paper content, pair this skill with your literature search skills instead of trusting embeddings alone.
- When retrieval is used for research writing, keep a clean separation between:
  source evidence, extracted notes, and generated synthesis.

## Practical checklist

- Corpus boundaries documented
- Metadata schema documented
- Chunking rule documented
- Benchmark queries saved
- Retrieval errors categorized
- Rebuild path tested with the correct Python
