# Retrieval evaluation

This is the method for deciding whether a retrieval change is an improvement.
Every retrieval change (hybrid search, reranking, a new embedding model,
different chunking) is scored on the same eval set, and every score is kept
in a ledger so progress can be compared over time.

```text
eval set (JSONL, can stay private)  ──►  run  ──►  ledger (IDs + numbers only)  ──►  compare
            ▲
     pool / resolve  (build and extend the eval set)
```

The tool is `benchmarks/scripts/retrieval_eval.py`. It reads the same active
canonical papers generation that the MCP server serves, configured through
`.env` / `LOCALRAG_*`.

## What is measured

Gold evidence is pinned to **canonical source coordinates**: PDF file hash,
physical page index, canonical page-text hash and a character interval. It is
never pinned to chunk IDs. The same eval set therefore scores any chunker,
embedding model or retrieval strategy that serves the same extracted pages.
If extraction changes a page's text, that evidence is reported as
*unscorable* rather than silently counted as a miss.

| Metric | Meaning |
|---|---|
| `span_coverage@k` (**primary: k=10**) | Share of gold spans fully contained in the union of the top-k verified hits (micro-averaged over spans). This is the metric the maintainer's W6 measurement used. |
| `span_touch@k` | Share of gold spans that overlap at least one top-k hit. |
| `group_coverage@k` | Share of evidence groups with at least one covered span. |
| `query_complete@k` | Share of queries whose every evidence group is covered. It approximates "the agent had everything it needed". |
| `span_coverage_macro@k` | Per-query coverage averaged over queries. |
| `doc_recall@k` | Share of relevant papers (by Zotero parent key) present in the top-k. |
| `mrr` | Reciprocal rank of the first hit overlapping any gold span. |
| `packet_span_coverage`, `packet_complete` | The same span and group coverage for the `prepare_answer` evidence packet at the given budget (default 8,000 code points). This is what an answering agent actually receives. |
| `latency_seconds` | p50/p95/max per search call, plus the first call. |
| `stability` | Queries whose top-k set (and order) is identical across repetitions. |

Only evidence with relevance ≥ 2 is scored. Relevance 1 marks useful background.

**Groups** are alternatives. Put spans in the same group when any one of them
is enough; for example, the same value stated in the main text and in the SI.
Use different groups for facts that are all required. A query is complete only
when every group is covered.

## Eval set format

A JSONL file with one query per line:

```json
{"query_id": "w6-l03", "text": "PtBi 在碱性条件下的 ORR 活性是多少？",
 "second_query": null,
 "filters": {},
 "slices": ["single-paper", "zh-to-en", "exact-value"],
 "relevant_parents": ["ABCD1234"],
 "evidence": [
   {"evidence_id": "w6-l03-e1", "group": "activity", "relevance": 3,
    "file_hash": "<sha256 of the PDF file>", "pdf_page_index": 4,
    "page_text_hash": "<canonical page_text_hash>",
    "char_start": 1520, "char_end": 1688,
    "zotero_parent_key": "ABCD1234"}]}
```

- `query_id`, `evidence_id`: letters, digits, `.`, `_`, `-`.
- `filters` (optional): `zotero_parent_key`, `zotero_attachment_key`,
  `source_role`, `pdf_filename`. Leave it empty to measure open retrieval.
- `slices` (optional): free labels for per-slice breakdowns, for example
  `exact-value`, `si-only`, `multi-hop`, `multi-paper`, `zh-to-en`, `negative`.
- `relevant_parents` (optional): papers that should be found; parent keys of
  evidence entries are added automatically.
- A query with no evidence is allowed: it contributes latency and stability
  and can hold negative (no-answer) cases.

Instead of coordinates, an evidence entry may carry a `quote` plus a scope
(`zotero_parent_key` or `file_hash`, optionally `source_role` and
`pdf_page_index`). `resolve` turns quotes into coordinates.

**Privacy.** Keep private eval sets outside the repository, for example
`~/research-rag-eval/w6-v2.jsonl`. They contain your questions and library keys.
The ledger stores only query IDs, settings and numbers.

## Building an eval set

### Route A — convert an existing gold set

If gold spans already exist in canonical coordinates, as for the W6-v2 suite
behind [the canonical release report](reports/CANONICAL_RELEASE.md), map their
fields to the format above. Verify with a `run`: every span should be scorable.

### Route B — quote annotation (fastest to write)

Write each query with verbatim quotes copied from the PDF text:

```json
{"query_id": "q01", "text": "...", "evidence": [
  {"evidence_id": "q01-e1", "group": "value", "zotero_parent_key": "ABCD1234",
   "source_role": "si", "quote": "overpotential of 240 mV at 10 mA cm-2"}]}
```

```bash
python benchmarks/scripts/retrieval_eval.py resolve --suite draft.jsonl --output eval.jsonl
```

Quotes match exactly, or with whitespace differences ignored. Each quote must
occur exactly once in its scope and lie on one page. Split cross-page evidence
into two entries in the same group. Unresolved quotes are listed, and the
command exits non-zero.

### Route C — pooled judging (best for finding what retrieval misses)

```bash
python benchmarks/scripts/retrieval_eval.py pool --suite questions.jsonl \
    --strategy dense --depth 20 --output pool.jsonl
```

Each query receives `candidates` with the passage text, pages and
`relevance: null`. For each candidate, set `relevance` (0–3). For relevant
candidates, set `quote` to the **minimal** supporting text inside it, and
optionally a `group`. Then:

```bash
python benchmarks/scripts/retrieval_eval.py resolve --suite pool.jsonl --output eval.jsonl
```

Quotes are required so that gold spans do not inherit one chunker's
boundaries. `--allow-chunk-spans` accepts whole candidates when you
deliberately accept that bias. When new strategies exist, pool again with
several `--strategy` flags so that gold is not limited to what dense retrieval
could already find. Always add evidence that you know exists but no strategy
retrieved (Route B).

### Annotation guidelines

- Aim for **at least 50 queries** and grow toward 100. With about 30 queries,
  only large differences (≥10 points) are distinguishable from noise.
- Mix query types in proportions that reflect real use:
  - exact values and units;
  - SI-only facts;
  - multi-hop main + SI;
  - comparisons across papers;
  - Chinese questions over English papers;
  - mechanism questions;
  - negative or no-answer cases.
  Tag them with `slices`.
- Write the question before looking at retrieval results, so the wording is
  not copied from the passage.
- Keep a **held-out split**: tune on one file (for example `eval-dev.jsonl`)
  and confirm on another (`eval-test.jsonl`) that you run rarely. Record both
  with different `--suite-id`s.
- Freeze a version once scores are recorded. Any edit changes the suite hash,
  and `compare` warns when runs used different versions.

## Recording scores

```bash
python benchmarks/scripts/retrieval_eval.py run --suite ~/research-rag-eval/w6-v2.jsonl \
    --suite-id w6-v2 --strategy dense --label "baseline qwen3-embedding:4b"
```

Defaults: k = 5, 10, 20; three repetitions per query; packet budget 8000;
unfiltered search without expanded context. These match the W6 measurement.

Each run appends one record to `benchmarks/results/retrieval-ledger.jsonl`
(`--ledger` to change). A record contains:

- the run ID and time;
- the git commit and whether the tree was dirty;
- the eval-set ID and hash;
- the papers generation ID and embedding provider, model, revision and
  dimensions;
- the strategy and its parameters;
- the metrics, overall and per slice;
- latency and stability;
- the unscorable evidence IDs;
- per-query numbers.

It never contains query text, passage text, paths or parent keys. The ledger
is meant to be committed, so scores stay attached to the code history. Use
`--details run.json` to keep the full record locally as well.

## Comparing

```bash
python benchmarks/scripts/retrieval_eval.py compare --suite-id w6-v2
python benchmarks/scripts/retrieval_eval.py compare --suite-id w6-v2 --baseline <run_id> --last 5
```

The table shows each run's headline metrics and, against the baseline, the
paired per-query delta of the primary metric. It includes a bootstrap 95%
interval and wins/losses/ties.

## Decision rule for retrieval changes

Change one thing per run: the strategy, the embedding model, the chunking or
the reranker.

1. Run the baseline and the candidate on the same eval-set version.
   - For strategy-only changes, also use the same papers generation.
   - An embedding or chunking change needs its own candidate generation.
     Build it under a separate `LOCALRAG_CHROMA_PATH` so the active index is
     untouched. Keep the extractor unchanged, or gold spans become unscorable.
2. Adopt a change when all of the following hold. These are proposed
   defaults; tighten them as the eval set grows.
   - the primary metric's 95% interval lies above zero, or the gain is at
     least 5 points with more wins than losses;
   - `query_complete@10` and `packet_span_coverage` do not regress;
   - p95 latency stays within the agreed budget (proposed: 2 s per search on
     the reference machine).
3. Confirm on the held-out split before changing a default. Record the
   confirming run's ID in the pull request.

## Relation to `benchmarks/`

The official public suites (S5 and later D20/V20/H60) use the stricter
contract in [`benchmarks/README.md`](../benchmarks/README.md). `--official s5`
reads those ledgers directly once their gold is adjudicated. Until then,
private eval sets such as W6-v2 carry the measurement. Their scores are
comparable only with runs of the same set.
