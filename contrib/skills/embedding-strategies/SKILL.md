---
name: embedding-strategies
description: "Use when choosing, comparing, or tuning embedding models, chunking policies, or multilingual retrieval behavior, especially for local Ollama embeddings, PDF corpora, and research-note search."
---

# Embedding Strategies

Choose embeddings based on the retrieval task, not brand familiarity. In this environment, the best model is the one that retrieves the right passages from your actual corpus.

## Local defaults

- Default provider/model: in-process `fastembed` with `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`
- Optional Ollama endpoint: `http://localhost:11434/api/embeddings` (`qwen3-embedding:0.6b` default tier; `4b` upgrade tier)
- Vector-store operations: `$LOCALRAG_RAG_PYTHON`
- General benchmarking scripts: `$LOCALRAG_MAIN_PYTHON`

## Use this skill when

- You are deciding whether to keep or replace `qwen3-embedding:4b`.
- Retrieval works in one language but not another.
- Chunking quality and embedding quality are being conflated.
- You need a fair benchmark instead of anecdotes.
- Embedding dimensionality, cost, or latency tradeoffs matter.

## Default evaluation loop

1. Freeze the task.
   - Decide whether you care about literature lookup, method retrieval, note discovery, or semantic clustering.
2. Freeze the benchmark.
   - Build a representative query set before comparing models.
3. Hold chunking constant.
   - Compare embeddings on the same corpus slices first.
4. Measure retrieval, not vibes.
   - Review top-k hits manually and log failure modes.
5. Only then compare chunking variants.
   - A better model cannot fully rescue broken segmentation.

## Practical advice for this environment

- Keep the configured production model as the baseline; compare Ollama `0.6b`/`4b` or remote providers only against a fixed benchmark.
- For bilingual Chinese/English corpora, test language-mixed queries explicitly.
- If note search and PDF search behave differently, consider separate embeddings or separate collections rather than one compromise setting.
- Prefer reproducible comparison tables over one-off impressions.

## What to log

- Model name and version
- Corpus snapshot used
- Chunking rule
- Query set
- Top-k recall observations
- Typical false positives
- Latency and throughput notes

## Anti-patterns

- Swapping models without a benchmark set
- Comparing models while chunking also changes
- Using a great benchmark on toy text that does not resemble your PDFs
- Assuming bigger dimensions automatically means better retrieval
- Mixing generated summaries into the benchmark as if they were ground truth
