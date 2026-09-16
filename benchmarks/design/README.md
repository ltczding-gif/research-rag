# Benchmark design index

This directory contains reviewed design proposals only. It is not a scratch space
and it does not contain active ResearchQA data or derived annotations.

| Artifact | Status | Purpose | Promotion gate |
|---|---|---|---|
| [`S5_QUERY_DESIGN.md`](S5_QUERY_DESIGN.md) | Shelved 2026-07-28 | Historical difficulty rubric and 25 S5 question specifications | Requires a future ADR to reactivate |
| [`ADR-003`](../../docs/plans/2026-07-28-researchqa-benchmark-adr.md) | Active | ResearchQA-only benchmark, four scale tiers, metrics and iteration order | Implement the pinned adapter and first baseline |

Directory rules:

- keep one canonical file per active design artifact;
- do not create replacement ResearchQA questions; use every upstream question
  belonging to each selected paper;
- do not commit downloaded benchmark datasets, PDFs, agent traces, temporary
  tables, or generated rewrites here;
- keep external benchmark references inside the design artifact that uses
  them;
- place executable schemas under `benchmarks/schemas/`, adjudicated records
  under `benchmarks/queries/` and `benchmarks/gold/`, and run outputs under
  ignored or intentionally published `benchmarks/reports/`.
