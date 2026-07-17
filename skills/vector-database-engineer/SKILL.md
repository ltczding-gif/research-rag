---
name: vector-database-engineer
description: "Use when selecting, tuning, or operating vector databases and semantic indexes, especially for Chroma-based local research pipelines, metadata filters, recall/latency tradeoffs, or collection design."
---

# Vector Database Engineer

Treat the vector store as infrastructure, not a black box. Collection design, metadata, and refresh strategy usually matter more than model swapping.

## Local environment

- LocalRAG Python: `$LOCALRAG_RAG_PYTHON`
- Main Python: `$LOCALRAG_MAIN_PYTHON`
- Chroma version target: `1.5.5`
- Chroma root: `$LOCALRAG_HOME\chroma`
- Default embedding backend: in-process `fastembed`
- Optional Ollama tier: `qwen3-embedding:0.6b` by default; `4b` for larger-memory hosts

## Use this skill when

- Chroma collections are slow, noisy, or hard to refresh.
- You need a collection schema for PDFs, notes, snippets, or multimodal metadata.
- Metadata filtering is missing or unreliable.
- You need to tune for recall vs. latency instead of guessing.
- A local research database needs safer ingestion or rebuild rules.

## Chroma-first workflow

1. Map entities before data.
   - Decide whether each collection stores full documents, sections, chunks, notes, or links between them.
2. Design metadata that supports filtering.
   - Stable keys: corpus, source file, parent document, section, language, note type, ingest time.
3. Separate collections by semantics when needed.
   - Raw PDF chunks and human-written notes often deserve separate collections.
4. Benchmark representative queries.
   - Test exact phrase lookups, conceptual lookups, and multilingual lookups separately.
5. Tune in the right order.
   - First: metadata and chunking
   - Second: collection boundaries
   - Third: embedding choice
   - Fourth: reranking or post-processing
6. Plan refresh and rollback.
   - Know how a collection is rebuilt, how duplicates are avoided, and how stale entries are removed.

## What to optimize

- Precision:
  reduce cross-document contamination and wrong-corpus matches.
- Recall:
  ensure relevant material survives chunking and filtering.
- Explainability:
  every returned item should retain enough metadata to trace back to the source.
- Rebuild safety:
  ingestion should be idempotent or at least auditable.

## Chroma-specific guardrails

- Prefer explicit collection naming over one generic catch-all collection.
- Keep metadata filterable with simple scalar values where possible.
- Track ingestion state outside the collection when the workflow depends on batches or PDF groups.
- Use the LocalRAG Python for any script that imports or mutates Chroma collections.

## Anti-patterns

- Re-ingesting the same corpus without duplicate strategy.
- Storing derived notes and raw sources with identical metadata.
- Optimizing on one or two demo queries.
- Hiding bad indexing behind higher `k`.
- Letting collection names and schemas drift across scripts.
