# research-rag benchmark

This directory is the public, isolated evaluation plane for RAG changes. It
never reads a user's Zotero database, note vault, production Chroma
collections, ledgers, or query logs.

## Active benchmark: ResearchQA

ADR-003 makes
[ResearchQA](https://huggingface.co/datasets/khoj-ai/ResearchQA) the only
active benchmark for the current optimization cycle. The repository does not
design replacement questions or mix additional public benchmarks into the
score.

The source contract is pinned in
[`sources/researchqa.yaml`](sources/researchqa.yaml):

- upstream revision:
  `33f3d7a83a1ae61511b4e3bfadab2f866eff2a03`;
- 494 papers and 6,211 upstream questions;
- ten measured domains;
- `lookup`, `comprehension`, `multi_hop`, and `adversarial` question types;
- exact source byte count and SHA-256;
- CC-BY-NC-4.0, external-only redistribution boundary.

Four deterministic, nested tiers select papers independently inside every
domain and keep every upstream question for each selected paper:

| Tier | Papers per domain | Total papers | Questions | Role |
|---|---:|---:|---:|---|
| `rq-2` | 2 | 20 | 254 | pipeline smoke and failure inspection |
| `rq-5` | 5 | 50 | 638 | first cross-domain iteration |
| `rq-10` | 10 | 100 | 1,263 | broader confirmation |
| `rq-all` | all | 494 | 6,211 | full public benchmark report |

The tiers use the same pinned seed and paper ranking, so
`rq-2 ⊂ rq-5 ⊂ rq-10 ⊂ rq-all`. A five-paper tier would not cover the ten
domains and is therefore not used.

The normative decision, metrics, leakage policy, and single-variable
iteration order are in
[`docs/plans/2026-07-28-researchqa-benchmark-adr.md`](../docs/plans/2026-07-28-researchqa-benchmark-adr.md).

## Prepare the four tiers

Install the benchmark-only dependencies:

```bash
python -m pip install -r requirements-benchmark.txt
```

Download the exact pinned ResearchQA JSONL, verify it, and generate all tier
indexes:

```bash
python benchmarks/scripts/prepare_researchqa.py
```

The command produces:

```text
benchmarks/.cache/researchqa/
  source/eval_dataset.jsonl
  index.json
  suites/
    rq-2/{suite.json,papers.jsonl,questions.jsonl}
    rq-5/{suite.json,papers.jsonl,questions.jsonl}
    rq-10/{suite.json,papers.jsonl,questions.jsonl}
    rq-all/{suite.json,papers.jsonl,questions.jsonl}
```

The whole output tree is ignored by Git. Repeating the command with the same
source and config must produce byte-identical indexes. The generator fails
closed if the upstream bytes, SHA-256, paper count, question count, domain
distribution, or question-type distribution changes.

Verify an already cached or explicitly supplied source without network access:

```bash
python benchmarks/scripts/prepare_researchqa.py --offline --check-only
python benchmarks/scripts/prepare_researchqa.py --source /path/to/eval_dataset.jsonl
```

## Active quality baseline

[`configs/baseline-fixed-800.yaml`](configs/baseline-fixed-800.yaml) freezes
the first ResearchQA quality baseline:

- canonical pdfplumber page IR with content-stream reading order
  (`use_text_flow=true`, `x_tolerance=1`);
- fixed-character C0 chunks: size 800, step 700, minimum 100;
- Ollama `qwen3-embedding:4b`, pinned by model digest;
- cosine dense retrieval with `top_k=10`;
- no reranker.

The product's smaller FastEmbed model remains useful for first-run smoke
tests, but its scores must not be mixed with the Qwen quality baseline.

## Evaluation boundary

ResearchQA supplies the questions, expected answers, evidence alternatives,
multi-hop section requirements, and adversarial refusal metadata. The
benchmark adapter must map each expected evidence alternative to canonical
PDF page/span coordinates before scoring candidate chunks.

The versioned evidence adapter normalizes NFKC alphanumeric text, locates an
alternative in the canonical page, and projects the exact character range
through each chunk's source spans. ResearchQA page and section hints are used
only as bounded fallbacks for known edition drift; they select the
best-matching chunk rather than marking a whole page or section relevant.

Retrieval and answer generation are evaluated separately. Reports must include
per-domain and per-question-type results, especially multi-hop and
adversarial performance; one global average is insufficient.

The current `rq-2` strategy sweep also generates one audited
`generic-research-note` per paper from the Main PDF plus every acquired
official SI/auxiliary file. Note chunkers and note-based source compositions
are evaluated by how well they recover ResearchQA's Main-PDF evidence groups.
ResearchQA does not provide separate SI or generated-note gold annotations,
and the PDF chunking arm loads only the `Main` benchmark PDF. Therefore this
cycle evaluates SI-informed notes, not direct SI/native-source retrieval.

The Qwen3 reranker is loaded with its declared `bfloat16` inference dtype.
That dtype is part of the candidate cache identity, so results from a
different numeric precision cannot be mixed into the same comparison.

`benchmarks.runtime.create_run_layout()` gives every run isolated home, notes,
Chroma, collection, ledger, query-log, artifact, and report paths.
`benchmarks.runtime.run_isolated()` removes inherited `LOCALRAG_*`, Zotero,
and model-cache settings before injecting run-owned state.

Public retrieval artifacts may expose only the fields allowed by
`benchmarks.public_report.sanitize_hits()`:

- `paper_id`
- `file_id`
- `pdf_page_index`
- `evidence_id`

Absolute paths, private text, private queries, API keys, and internal metadata
must not enter public reports.

## Data and license boundary

Do not commit:

- the ResearchQA JSONL or derived question subsets;
- downloaded PDFs or extracted text;
- chunks, embeddings, vector indexes, model caches, or raw run traces;
- quoted source passages without a separate license review.

ResearchQA annotations are CC-BY-NC-4.0. Quoted paper text and PDFs retain
their original rights. The repository license does not relicense those
external assets. Commit reproducible code, pinned fingerprints, aggregate
metrics, and sanitized reports instead.

## Frozen S5 track

The earlier S5 main-plus-SI corpus, generated-note fixtures, candidate query
design, legacy suites, and Wave 1A canonical-IR artifact are retained for
provenance and possible later revival. They are currently shelved:

- they do not block ResearchQA work;
- their empty gold/qrel ledgers are not filled during this cycle;
- their scores are not mixed with ResearchQA;
- no claim is made that ResearchQA annotations directly cover SI,
  generated-note evidence, cross-language retrieval, chemistry/materials,
  physics, or engineering.

The legacy contract remains checkable while it is frozen:

```bash
python benchmarks/scripts/validate_benchmark.py --allow-empty
```

The old `--release-ready` S5/D20/V20/H60/S100 gate is historical and is not a
completion gate for the ResearchQA cycle.
