# Benchmark design index

This directory contains reviewed design proposals only. It is not a scratch space
and it does not contain official gold annotations.

| Artifact | Status | Purpose | Promotion gate |
|---|---|---|---|
| [`S5_QUERY_DESIGN.md`](S5_QUERY_DESIGN.md) | Owner-review candidate | Difficulty rubric and 25 S5 question specifications | Main/SI re-annotation, exact evidence locators, second-person review, owner approval |

Directory rules:

- keep one canonical file per active design artifact;
- do not commit downloaded benchmark datasets, PDFs, agent traces, temporary
  tables, or generated rewrites here;
- keep external benchmark references inside the design artifact that uses
  them;
- place executable schemas under `benchmarks/schemas/`, adjudicated records
  under `benchmarks/queries/` and `benchmarks/gold/`, and run outputs under
  ignored or intentionally published `benchmarks/reports/`.
