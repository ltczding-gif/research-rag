# research-rag benchmark

This directory is the public evaluation plane described by ADR-002. It is
deliberately separate from the user's Zotero database, note vault, Chroma
collections, ledgers, and query logs.

## Current milestone

Wave 0A is complete: it defines and validates the data contract, provides the
behavior-preserving fixed-800 legacy PDF seam, creates isolated run-owned
state, and converts internal retrieval hits through a public allowlist. The
committed JSONL files and suite membership lists remain intentionally empty
until the S5 corpus is curated in Wave 0B.

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

Validate the committed empty Wave 0A skeleton:

```bash
python benchmarks/scripts/validate_benchmark.py --allow-empty
```

Once S5 data exists, omit `--allow-empty`. Before an official release run, use
the stricter quota and partition checks:

```bash
python benchmarks/scripts/validate_benchmark.py --release-ready
```

## Data boundary

Official S5/D20/V20/H60 data must have verified redistribution permission.
PDF binaries belong in a checksum-pinned release/dataset artifact, not Git
history. Private papers and download-only local extensions must not contribute
to public benchmark scores.

Generated caches, fetched files, run artifacts, and reports are ignored by Git.
Only reviewed contracts, annotations, suite definitions, and intentionally
published reports should be committed.
