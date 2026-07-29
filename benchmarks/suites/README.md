# Benchmark suite index

The YAML files in this directory are the frozen S5/D20/V20/H60/S100 contract
from ADR-002. They remain for provenance and legacy validation, but they are
not active optimization or release gates.

Current ResearchQA suites are generated deterministically from
[`../sources/researchqa.yaml`](../sources/researchqa.yaml):

| Active tier | Papers per domain | Total papers | Questions |
|---|---:|---:|---:|
| `rq-2` | 2 | 20 | 254 |
| `rq-5` | 5 | 50 | 638 |
| `rq-10` | 10 | 100 | 1,263 |
| `rq-all` | all | 494 | 6,211 |

Generated suite indexes and question subsets live under the ignored
`benchmarks/.cache/researchqa/suites/` tree. They are not committed because
ResearchQA is an external CC-BY-NC-4.0 evaluation asset.
