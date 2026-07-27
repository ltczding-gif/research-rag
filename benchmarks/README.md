# research-rag benchmark

This directory is the public evaluation plane described by ADR-002. It is
deliberately separate from the user's Zotero database, note vault, Chroma
collections, ledgers, and query logs.

## Current milestone

Wave 0A is complete: it defines and validates the data contract, provides the
behavior-preserving fixed-800 legacy PDF seam, creates isolated run-owned
state, and converts internal retrieval hits through a public allowlist.

Wave 0B is in progress. Its first corpus-lock increment freezes five public
papers, one per domain, with a main PDF and SI PDF for each, source URLs,
CC BY 4.0 evidence, and SHA-256 checksums. See
[`corpus/SOURCES.md`](corpus/SOURCES.md) for the selection and license record.
Five main-plus-SI candidate notes are also frozen under
[`fixtures/generated_notes/`](fixtures/generated_notes/). They were generated
through the repository's manifest-driven subagent protocol, not Gemini. The
four out-of-pack papers use the field-neutral generic contract; independently
audited P1 corrections retain separate repair provenance in the fixture
manifest. Every note remains explicitly marked `human_review_status: pending`;
none is gold. The query, answer, claim, evidence-unit, and qrel ledgers remain
empty until a human reviews the notes and the 25 S5 queries are adjudicated.

The benchmark design workbench is indexed in
[`design/README.md`](design/README.md). Its current S5 proposal defines the
25-query allocation, hard/very-hard rubric, wording, negative cases, claim IDs,
and evidence-group targets. It is deliberately not copied into the official
ledgers yet: source quotes, canonical page locators, second-person review, and
owner approval are required first.

The contract covers:

- redistributable PDF/SI metadata and checksums;
- stable query-level answer keys and claims;
- chunk-independent evidence units;
- document/evidence qrels and judgment pools;
- S5, D20, V20, H60, and combined S100 suite membership;
- versioned run reports.

Candidate chunks must be mapped to stable `evidence_id` values by the evaluator.
Gold records must never contain a candidate `chunk_id`.
Human judgments follow [ANNOTATION_GUIDE.md](ANNOTATION_GUIDE.md).

## Runtime boundary

`benchmarks.runtime.create_run_layout()` validates the requested `run_root`
against explicit production paths before creating anything. Each accepted run
receives its own home, notes directory, Chroma path, collection names, ledgers,
query log, model cache, artifacts, and reports directory.

`benchmarks.runtime.run_isolated()` runs a command without a shell. It removes
inherited `LOCALRAG_*`, Zotero, and model-cache settings before injecting the
run-owned environment. Callers must supply the production paths to protect;
missing home, notes, Chroma, ledger, query-log, or Zotero boundaries fail
closed.

Only the four fields emitted by `benchmarks.public_report.sanitize_hits()` may
represent retrieved evidence in a public artifact:

- `paper_id`
- `file_id`
- `pdf_page_index`
- `evidence_id`

Absolute paths, source text, private queries, scores, API keys, and internal
metadata are discarded rather than redacted heuristically.

The C0 production adapter and future benchmark runner share
`service/pdf_baseline.py`. Importing that module performs no vault scan, PDF
read, Zotero lookup, Chroma initialization, or collection binding.

## Validate

Install the benchmark-only dependencies:

```bash
python -m pip install -r requirements-benchmark.txt
```

Validate the current contract and corpus lock while downstream gold ledgers
are still being built:

```bash
python benchmarks/scripts/validate_benchmark.py --allow-empty
```

Once all S5 gold ledgers exist, omit `--allow-empty`. Before an official
release run, use
the stricter quota and partition checks:

```bash
python benchmarks/scripts/validate_benchmark.py --release-ready
```

## Data boundary

Official S5/D20/V20/H60 data must have verified redistribution permission.
PDF binaries belong in a checksum-pinned release/dataset artifact, not Git
history. Private papers and download-only local extensions must not contribute
to public benchmark scores.

Every official S5 paper must provide both its main PDF and at least one
redistributable SI PDF. The validator rejects an S5 paper whose manifest has an
empty `si` list; this requirement is deliberately scoped to S5 because the
larger suites must also measure papers that genuinely have no SI.

Generated caches, fetched files, run artifacts, and reports are ignored by Git.
Only reviewed contracts, annotations, suite definitions, and intentionally
published reports should be committed.

Frozen generated-note fixtures are the deliberate exception: they are
path-sanitized, checksum-pinned candidate outputs used by offline evaluation.
Their provenance is recorded in
[`fixtures/generated_notes/manifest.jsonl`](fixtures/generated_notes/manifest.jsonl).
Human corrections must create separate files under `gold/notes/` rather than
mutating the generated baseline.

## Acquire or verify the corpus

The manifest is sufficient to reacquire the selected publisher files:

```bash
python benchmarks/scripts/fetch_corpus.py
```

Verify an existing local copy without network access:

```bash
python benchmarks/scripts/fetch_corpus.py --check-only
```

The files land under ignored `benchmarks/corpus/files/` paths. This command is
for maintainer acquisition and manual verification. Ordinary pull-request CI
must use a checksum-pinned offline corpus artifact; it must not depend on live
publisher downloads.
