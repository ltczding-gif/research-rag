# Benchmark source index

| Source | Status | Contract | Local data |
|---|---|---|---|
| ResearchQA | Active and pinned | [`researchqa.yaml`](researchqa.yaml) | Generated under ignored `benchmarks/.cache/researchqa/` |
| S5 public corpus | Shelved historical track | [`../corpus/manifest.jsonl`](../corpus/manifest.jsonl) | Existing ignored `benchmarks/corpus/files/` |

ResearchQA is the only benchmark source used for current tuning and reporting.
The committed contract fixes its upstream revision, file hash, measured
distribution, license boundary, deterministic selection seed, and the
per-domain 2/5/10/all tiers.

Run `python benchmarks/scripts/prepare_researchqa.py` to build the local source
and suite indexes. Do not commit the downloaded JSONL, derived questions, or
paper PDFs.
