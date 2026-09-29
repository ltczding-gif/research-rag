# Documentation

Start with the [README](../README.md). The documents below are current and
maintained alongside the code.

## Using research-rag

| Document | Covers |
|---|---|
| [ANSWER_WORKFLOW.md](ANSWER_WORKFLOW.md) | `prepare_answer` / `check_answer`: bounded evidence packets and citation checks |
| [EVIDENCE_CONTEXT.md](EVIDENCE_CONTEXT.md) | How source-coordinate context windows are assembled around a match |
| [INDEX_GENERATIONS.md](INDEX_GENERATIONS.md) | Immutable index generations, migration, removals and rollback |
| [LOCAL_DEPLOYMENT.md](LOCAL_DEPLOYMENT.md) | Building a candidate, verifying the real client entrypoint, recovery |
| [Domain_Pack_Authoring_Guide.md](Domain_Pack_Authoring_Guide.md) | Creating a domain pack for a new field |

## Internals

| Document | Covers |
|---|---|
| [development/EMBEDDING_BUILD_SESSIONS.md](development/EMBEDDING_BUILD_SESSIONS.md) | Provider identity binding during builds and its limits |
| [development/GENERATION_RECOVERY.md](development/GENERATION_RECOVERY.md) | Repairing a damaged active-generation pointer |
| [../skills/literature-processor/references/subagent-host-contract.md](../skills/literature-processor/references/subagent-host-contract.md) | The exit-code-200 sub-agent manifest protocol |
| [RETRIEVAL_EVAL.md](RETRIEVAL_EVAL.md) | Building eval sets, scoring retrieval strategies and keeping a score ledger |
| [../benchmarks/README.md](../benchmarks/README.md) | Public retrieval benchmark contract (in progress) |

## Validation reports

[`reports/`](reports/) holds the maintainer's measured results on a private
library of about 2,100 papers: coverage accounting, build recovery, retrieval
evidence coverage and real-client answer review. They document measured limits;
they are not setup instructions.

- [reports/FULL_LIBRARY_VALIDATION.md](reports/FULL_LIBRARY_VALIDATION.md)
- [reports/CANONICAL_RELEASE.md](reports/CANONICAL_RELEASE.md)

## Archive

[`archive/`](archive/) keeps design records, audits and status snapshots from
earlier development. They are historical and may contradict the current code.
