# W6 reranker evaluation — 2026-09-30

## Decision

**Do not enable reranking by default.** On this development set, plain hybrid
retrieval still gives the best top-10 source-span coverage: **21/31**. BGE
reranking improves early precision proxies, but hybrid-rerank drops to **20/31**,
loses cross-paper completeness, and has a measured p95 of **2.047 s**.
Dense remains the shipped default; neither strategy has held-out confirmation.

This is a retrieval measurement, not an end-to-end answer-accuracy benchmark.
The records are in the [retrieval ledger](../../benchmarks/results/retrieval-ledger.jsonl);
the scoring contract is in [RETRIEVAL_EVAL.md](../RETRIEVAL_EVAL.md).

## Experiment and provenance

- PR [#26](https://github.com/ltczding-gif/research-rag/pull/26) was reviewed,
  squash-merged and evaluated at `0c8907efe1ac826bfa67dba5e3c777594713d33c`.
  Its 12 CI checks passed. Local tests: **538 passed, 3 skipped**.
- All four runs used that same clean tracked-code revision, the same immutable
  papers generation, the same sliced suite, and the same cached query vectors.
  Records were written outside the checkout and appended together afterward,
  so writing one record did not mark later measurements dirty.
- Generation: `ae58ebde4e7b4e798c86d777873c0a46`, **161,225 chunks**.
- Embedding: existing `qwen3-embedding:4b`, 2,560 dimensions; no re-embedding or
  index rebuild. Query-vector SHA-256:
  `159168719a267e09fe920256fcf0e14674f110c74576427128812a92c60c8971`.
- Suite SHA-256:
  `56779508f51ef636f637e90cd0f928620f50ba65eec5050dd36a78321a2cee9b`.
  **30 questions, 22 with gold, 31 scored spans, zero excluded/unscorable spans**.
  The other eight negative questions measure latency/stability only: this
  evaluator does not score refusal or hallucination on them.
- Slices: 10 single-paper questions / 11 spans; 6 cross-paper / 14 spans;
  6 SI-only / 6 spans; 8 negative / zero spans. The existing question texts and
  coordinates were not edited. None uses `second_query`.
- The original gold source labels all 30 questions `agent_authored`,
  `human_reviewed=false`, `pending_independent_review`. Coordinate validity
  does **not** establish independent semantic correctness of the gold.

Run IDs (timestamps are UTC; report date is Asia/Shanghai):

| Strategy | Run ID |
|---|---|
| dense | `w6-v2-dense-20260929T174842Z-509bfe` |
| hybrid | `w6-v2-hybrid-20260929T175018Z-f805a8` |
| dense-rerank | `w6-v2-dense-rerank-20260929T175319Z-d00e35` |
| hybrid-rerank | `w6-v2-hybrid-rerank-20260929T175735Z-422698` |

## What each strategy does, and why

| Strategy | Actual pipeline | Intended benefit |
|---|---|---|
| dense | Query vector → canonical Chroma passages, ordered by vector distance | Semantic baseline; verify the earlier 17/31 result |
| hybrid | Dense top 50 + SQLite FTS5/BM25 top 50 → reciprocal rank fusion, k=60 | Recover exact terms and SI evidence that dense search misses |
| dense-rerank | Dense top 50 → BGE scores original question/passage pairs → stable descending order | Move semantically relevant but low-ranked evidence into top 10 |
| hybrid-rerank | Same hybrid pool → fused top 50 → the same BGE reranker | Combine wider recall with stronger ranking |

Reranking never adds passages. Scores are rounded to five decimal places before
sorting; ties keep first-stage order. Every scored source span still undergoes
canonical coordinate verification. Three repetitions per question check rank
stability; answer packets use top 10 with an 8,000-code-point budget.

Diagnostics request depth 100 but pin the scored candidate pools. For reranked
strategies this yields **at most 50 results**, not an expanded 100-document
reranker pool. Thus their ledger `span_coverage@100` is the coverage of that
fixed 50-result pool. Prefix checks passed for every query.

## Results

| Strategy | Coverage@5 | Coverage@10 | Coverage@20 | Complete questions@10 | MRR | Packet coverage | p50 / p95 seconds |
|---|---:|---:|---:|---:|---:|---:|---:|
| dense | 13/31 | **17/31** | 22/31 | 12/22 | .511 | 17/31 | .111 / .154 |
| hybrid | 17/31 | **21/31** | 23/31 | 16/22 | .478 | 21/31 | .507 / .719 |
| dense-rerank | 18/31 | **19/31** | 23/31 | 15/22 | .643 | 19/31 | 1.126 / 1.381 |
| hybrid-rerank | 20/31 | **20/31** | 24/31 | 15/22 | .681 | 20/31 | 1.614 / 2.047 |

All four strategies had identical top-20 membership **and order in 30/30
questions across three repetitions**. No reranker request failed; server logs
showed no truncated passages. These checks establish this run's stability,
not universal GPU determinism.

Latency is retrieval-only (including HTTP reranking), excluding query embedding.
This was one fixed-order batch on a busy Windows desktop, not randomized or
isolated performance testing. First-call/max times were 2.069, 1.424, 1.912 and
3.746 seconds respectively. The hybrid-rerank p95 slightly exceeds the proposed
2-second budget; it is not evidence of a precise portable performance limit.

### Paired uncertainty and the averaging trap

| Comparison | Micro span change@10 | Mean per-question change | Paired bootstrap 95% CI | Wins / losses / ties |
|---|---:|---:|---:|---:|
| hybrid − dense | +12.90 pp | +9.85 pp | −2.27 to +22.73 pp | 4 / 1 / 17 |
| dense-rerank − dense | +6.45 pp | +6.82 pp | −2.27 to +18.18 pp | 3 / 1 / 18 |
| hybrid-rerank − dense | +9.68 pp | +10.61 pp | −2.27 to +25.00 pp | 4 / 1 / 17 |
| hybrid-rerank − hybrid | **−3.23 pp** | **+0.76 pp** | −10.61 to +13.64 pp | 2 / 2 / 18 |

The CLI's paired delta averages **questions**; headline coverage averages
**spans**. They can point in opposite directions: hybrid-rerank improves two
questions but loses three spans across two others. Its small positive paired
mean must not be presented as an improvement in the primary micro coverage.
Every interval crosses zero; 22 scorable questions are insufficient for strong
generalization claims. Dense-rerank passes the development screening heuristic
against dense, but is worse than plain hybrid on primary coverage. Incremental
reranking over hybrid fails coverage/completeness/packet guardrails.

### Slice coverage@10

| Slice | dense | hybrid | dense-rerank | hybrid-rerank |
|---|---:|---:|---:|---:|
| Single-paper, 11 spans | 8/11 | 8/11 | 9/11 | 9/11 |
| Cross-paper, 14 spans | 4/14 | **7/14** | 5/14 | **5/14** |
| SI-only, 6 spans | 5/6 | 6/6 | 5/6 | 6/6 |

## Where the evidence moved

These are all questions missed at top 10 by at least one strategy. Numbers are
the first ranks at which gold intervals are fully covered. `—` means absent
from the diagnostic list: up to 100 for the non-reranked strategies, at most
the fixed 50 for reranked ones. Arrays are **sorted ranks, not stable evidence
IDs**; array positions must not be used to identify the same span across runs.

| Question | dense | hybrid | dense-rerank | hybrid-rerank |
|---|---|---|---|---|
| C01 | 8, — | 15, — | 21, — | 24, — |
| C02 | 1, 24 | 2, 5 | 1, 2 | 1, 2 |
| C03 | 2, 17 | 1, 10 | 1, 10 | 1, 12 |
| C04 | —, —, — | 40, —, — | —, —, — | 5, —, — |
| C05 | 7, 11, 18 | 4, 5, 8 | 2, 12, 15 | 2, 12, 16 |
| C06 | —, — | —, — | —, — | —, — |
| L03 | 29 | 26 | 17 | 28 |
| L08 | 15 | 12 | 1 | 1 |
| L09 | 11 | 25 | 13 | 14 |
| S01 | — | 4 | — | 2 |

L01, already covered, has ranks 1 / 2 / 2 / 2: reranking does not restore the
dense rank-1 position. C01 gets worse, not better.

### Distinguishing ranking, recall and evidence selection

1. **Successful promotion:** hybrid-rerank moves L08 from 12 to 1 and C04's
   available span from 40 to 5. C02's second required span moves from 5 to 2;
   S01 moves from 4 to 2. These explain the better early-rank metrics.
2. **Loss of complete multi-paper support:** C03 loses one top-10 span and C05
   loses two. These three lost spans outweigh the two newly covered spans.
   Better MRR measures an earlier first overlap, not possession of every fact.
3. **Correct paper is not enough:** a separate frozen-vector audit found L03
   still has three chunks from its gold paper in the reranked top 10, and L09
   has six, yet neither required interval is covered. This is passage/evidence
   selection failure, not simply failure to retrieve the paper.
4. **C05 concentration is observable, but not the universal cause:** before
   reranking, its two gold papers contribute 3 and 6 top-10 chunks, plus one
   other-paper chunk. Afterward they contribute 1 and 9; coverage drops from
   3/3 to 1/3. C03 instead shifts from five to seven chunks from non-gold papers.
   C01 shifts from six gold-paper chunks to **zero**. A generic diversity
   penalty alone would not address these different failure modes.
5. **Hard candidate-pool limit:** both hybrid's diagnostic union and the actual
   fused top-50 reranker pool cover **26/31 spans**; dense's reranker pool covers
   24/31. For hybrid-rerank the five absent spans are one in C01, two in C04,
   and two in C06. No permutation of its existing candidates can recover them.
   Another six spans are present but remain beyond rank 10 after reranking.

The 26/31 figure is a **recall ceiling**, not proof that some ordering of ten
chunks can jointly cover all 26 spans. Current experiments do not compute that
combinatorial top-10 optimum. Nor do they prove why the neural scorer preferred
each distractor; architecture/training explanations remain hypotheses.

## Execution issues and how they were handled

- Reused the installed Ollama-bundled `llama-server`; no Python dependency or
  additional runtime was installed. Only model weights were downloaded.
- Standalone startup initially found no GPU. Pointing `GGML_BACKEND_PATH` to
  the existing `cuda_v12/ggml-cuda.dll` and prepending that directory to the
  child process's PATH exposed CUDA0. CPU-only startup was stopped before the
  recorded evaluations; both reranked runs used the GPU-backed server.
- Used the [Q8_0 conversion](https://huggingface.co/klnstpr/bge-reranker-v2-m3-Q8_0-GGUF/tree/efd0e18e4b943f4efa1b55dde134da760dbccf91)
  at revision `efd0e18e4b943f4efa1b55dde134da760dbccf91`; 635,676,416 bytes.
  The downloaded SHA-256 matched the publisher's LFS metadata:
  `a1c7499841b5f9f5d9ab2c74629293740dbdbe217ded4f0baa64f233ec34c5e4`.
  This identifies the conversion; it is not an F16-versus-Q8 equivalence test.
- Runtime: llama-server `0.4.1-dev`, build 1, commit `161755f29`; executable
  SHA-256 `258b1f3a315a76b6cdc82a76231378a446b33a30c1addb8448a4525eb7f27143`.
  RTX 3070 Laptop GPU, 8 GiB. Flags:
  `--reranking --pooling rank -ngl 99 -c 8192 -b 2048 -ub 2048 -np 1`.
  Server bound to loopback only; it was stopped after the measurements.
- Windows had little free commit memory. Cached query vectors avoided loading
  the 4B embedding model alongside Chroma and the reranker. The system-managed
  pagefile grew automatically; no system memory setting or existing MCP
  process was changed. The dense preflight reproduced 17/31 before formal runs.
- Local HTTP provider configuration was added with default reranking explicitly
  off. The ledger stores model identity/hash, never the endpoint. Private suite,
  vectors, source identifiers, server logs and model weights stay outside Git.

## Ordered follow-ups, not enabled strategies

1. **Independent confirmation first:** audit the W6 gold and finish a separate
   held-out set before any default change. A private deterministic selection
   draft now contains 24 main+SI paper groups drawn from 466 eligible groups,
   excluding the eight W6 families identifiable from hashes/structured IDs.
   It contains **no gold questions yet and is not frozen**. Check DOI/title
   duplicates and exposure through question text before annotation; do not
   claim cross-domain balance from this random material sample.
2. **Target the measured ranking failures:** test per-entity/subquestion
   retrieval and calibrated fusion of first-stage and reranker ranks. Test
   per-paper caps only as a separate C05 concentration ablation, not a blanket
   cure. Keep hybrid as the comparison baseline; preserve the same gold suite,
   candidate budget, and source-coordinate scoring.
3. **Treat absent evidence separately:** audit C01/C04/C06 recall failures,
   then compare query decomposition/rewriting and candidate budget changes.
   If adequate spans are still absent, inspect chunk boundaries and embedding
   recall. A larger pool is a new experiment, not a retrospective rerank win.
4. **Only then optimize cost:** rerank candidate budget/batching and runtime
   tuning should be separately measured after coverage/completeness is useful.
   Do not trade away semantic guardrails for a latency improvement.

To reproduce the comparison from the checked-in ledger:

```bash
python benchmarks/scripts/retrieval_eval.py compare --suite-id w6-v2 --last 4
```

To rerun, use the private sliced suite/vectors, the pinned model/runtime above,
and the four-strategy command in [RETRIEVAL_EVAL.md](../RETRIEVAL_EVAL.md#reranking).
Recheck generation/model hashes rather than assuming the local deployment is
still the one measured here.
