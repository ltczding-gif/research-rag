# research-rag benchmark

This directory is the public evaluation plane described by ADR-002. It is
deliberately separate from the user's Zotero database, note vault, Chroma
collections, ledgers, and query logs.

## Current milestone

Wave 0A defines and validates the data contract. The committed JSONL files and
suite membership lists are intentionally empty until the S5 corpus is curated
in Wave 0B.

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
