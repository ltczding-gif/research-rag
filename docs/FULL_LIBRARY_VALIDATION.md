# Full-library validation and rollout plan

This document preserves the Module 3 baseline and its original rollout scope.
Current Modules 4–5 source accounting and acceptance status are recorded in the
[canonical release report](CANONICAL_RELEASE.md).

Status: Module 3 implemented, 2026-09-17. **Operational retrieval passed; full-library
cited-answer quality acceptance is not yet passed.** Modules 1–2 provide the
canonical evidence/answer workflow, but the current production store is legacy.

## Scope and reproducibility

Validation used a complete isolated copy of the existing Chroma store, not a
paper-filtered collection: 150,244 PDF chunks and 2,140 note records. This means
the **entire indexed library**, not every attachment in Zotero. Production file
names, sizes and modification timestamps were unchanged at the end; this check
is not a final full-byte hash. No production index, active pointer, ledger, source
PDF or note was edited, and no answer-generation provider was called.

The run used Chroma 1.5.5, the existing dense retrieval path, local
`qwen3-embedding:4b`, top 10 PDF hits, neighbor context enabled, no source filters,
and three repetitions of each of 30 frozen diagnostic questions. The question
set has 22 answerable questions and eight negative controls **for its original
eight-paper corpus**. Those controls are not assumed unanswerable in the larger
library. They were authored for diagnosis, not independently sampled or labeled.

- Question suite SHA-256: `0b8c79e888da7523f26b8feeaf535b2eb5ed32f89ce035f2627ff6d0058c4324`.
- Current query-model digest: `df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907`.
- The legacy index did not record its build-time model digest. Current runs hold
  the query model fixed; historical model identity cannot be certified.
- Raw questions, responses, paths, inventories and source hashes remain in the
  private local acceptance workspace. Public reports contain aggregate results.

To repeat the indexed-inventory portion against a local snapshot:

```sh
python scripts/audit_library.py --chroma-path /path/to/chroma-snapshot --output inventory.json
```

Use the installed RAG environment. The command reads SQLite in read-only mode,
does not instantiate Chroma or contact a model, and does not open PDF contents.
It checks indexed source-path existence, not content validity. `--include-paths`
adds missing paths to a private report; paths are omitted by default. Schema
classification is a metadata hint, not a provenance-verification result.

## Inventory and coverage findings

| Observation | Result | Meaning |
|---|---:|---|
| Indexed PDF chunks / distinct parent keys | 150,244 / 1,855 | 956 chunks lack a parent key |
| Indexed PDF paths | 2,320 | 2,318 exist; two are unavailable at their stored paths |
| Indexed note records / distinct parent keys | 2,140 / 2,092 | Records are not necessarily one current note per paper |
| Union of indexed parent keys | 2,107 | PDF and note counts overlap |
| Canonical generation metadata | Absent in both collections | Legacy provenance remains unverified |
| Note source-path field | Present in 354 records | Absence does not establish missing notes; older metadata uses other fields |
| Diagnostic PDFs matched by current file bytes | 11 / 16 | Five exact target files have no matched indexed path |
| Answerable questions with all required target files represented | 12 / 22 | File presence alone does not prove relevant chunks are present |

The five unmatched targets are W6P03 Main (PtBi), W6P05 Main (IrW), W6P06 Main
(Co), W6P07 SI-01 and W6P08 Main. Matching inspected all 2,320 indexed PDF paths,
then hashed the 11 size-matched candidate files. It does not prove that equivalent
content or a different PDF version is absent elsewhere. The two unavailable
indexed paths also cannot be byte-verified.

This changes the priority of L03's missing ORR condition: its PtBi main PDF is not
represented by an exact matched indexed file. Repairing input coverage comes
before treating this as a query-ranking failure.

Separate read-only source inventories found 2,153 active Zotero parent items with
PDFs and 2,947 active PDF attachments. Local note frontmatter contains keys for
2,042 of those parents (94.84%). This is **key presence**, including historical
note versions, not note correctness or current searchable-index coverage. The
hash-only ledger contains 2,281 unique group hashes; hash-set differences cannot
be interpreted as failed ingestion without a source manifest.

## Concrete repairs

1. **Compact chunk IDs:** 32,917 production chunks use IDs ending in `_cN`.
   Context retrieval previously rejected these and could fail a complete MCP
   search. Neighbor lookup now supports this form and the older `_chunk_N` form,
   deriving the ordinal from the actual ID suffix.
2. **Cross-paper neighbor context:** 168 of 1,721 legacy ordinal prefixes map to
   multiple PDF paths and parent keys. Adjacency alone is therefore unsafe.
   Context now requires equal nonempty PDF paths and rejects conflicting parent,
   attachment, source hash, legacy file hash or file identity when both values
   are present. Neighbors without source metadata are omitted. Results remain
   explicitly unverified; this is not canonical source certification.

A real collision boundary was checked: the previous chunk belongs to a different
paper and is rejected, while the following same-source chunk is retained. The
snapshot proves prefix reuse; it does not establish why it occurred or whether
older chunks were overwritten. No destructive repair was attempted.

## Acceptance results and limits

- All **90 PDF MCP searches** completed after the repairs. Eight title-based
  note searches found the expected parent in the first five results (8/8); this
  is a narrow discovery smoke test, not general relevance accuracy.
- Across three repetitions, 23/30 questions returned the same top-10 ID **set**;
  the text-hash set count was also 23/30. Ordering is a separate matter. Ranking
  is not assumed deterministic.
- Warm repeated searches took about **35 ms median / 42 ms p95** on this machine;
  startup took 10.10 seconds and the first search took 2.30 seconds.
  This is a small sequential warm-cache test, not a concurrency/load benchmark.
- `prepare_answer` correctly refused this legacy store because it requires a
  canonical PDF generation. No full-library answer accuracy was measured.
- Only 21/344 inspected target chunks could be matched exactly to normalized
  canonical text layout. Legacy extraction interleaves columns, while current
  gold evidence uses canonical page coordinates. **Coordinate recall is
  unscorable here, not zero.** There is no valid same-protocol quality comparison
  with the earlier eight-paper canonical experiment and no claimed score gain.
- Regression coverage includes both ID formats, the real ordinal collision
  shape, missing neighbor identity, conflicting parent/hash metadata and the
  unaffected canonical context path. The inventory CLI has SQLite fixture tests
  for counts, field coverage, privacy, unchanged database bytes and invalid input.
  The final local suite completed with **476 passed, 3 skipped**.

Module 3 closes the inventory, operational validation and identified service
repairs. It leaves full-library quality acceptance open as an explicit migration
gate, rather than approving the current legacy library for reliable answers.

## Next modules

### Module 4 — candidate migration and product integration

1. Build a source manifest from current Zotero attachments, file bytes and note
   identity; reconcile missing paths/parents and the five diagnostic targets.
   Keep unresolvable rows in an exception list, preserving source files.
2. Extract canonical text with stable file identities into a separate candidate
   generation. Review column order and source windows on the problematic targets
   before embedding the full corpus. Do not reuse ordinal prefixes as identity.
3. Run a small throughput pilot, then estimate full-library build time from the
   observed chunk count and actual local model rate. Retain build checkpoints.
   A complete re-embedding is not included in Module 3's measurements.
4. Validate the candidate through real MCP: note discovery, source retrieval,
   answer preparation and citation checking. Freeze model, questions, corpus,
   source budget and judgments before comparing retrieval/answer quality.
5. Switch the product configuration only after acceptance, retaining a rollback
   target. Match the active generation's embedding contract and the actual note
   root; repository defaults currently differ from the existing local library.

Expected engineering work: **4–6 hours plus measured extraction/embedding time**.
The 150k-chunk store has not yet been rebuilt, so a fixed full migration deadline
would be unsupported. Source ambiguity, replacing production data or changing
model/provider policy requires a concrete decision before that action; ordinary
isolated implementation and validation can proceed autonomously.

### Module 5 — release closeout

Run a clean-install/client walkthrough, document normal operation and recovery,
and publish the accepted configuration and known limits. Expected engineering
work: **1–2 hours after Module 4's acceptance**. Completion requires an actual
production/client smoke test, not only a merged code change.
