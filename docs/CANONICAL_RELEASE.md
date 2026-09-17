# Canonical library release — Modules 4–5

Status: **acceptance in progress**, 2026-09-18. The source snapshot is dated
2026-09-17. The production configuration has not been switched. Build progress
and a passing code suite alone do not complete the release.

## Reconciled candidate inputs

The candidate uses 2,143 current notes from 2,152 top-level originals. All
original bytes are retained. Seventeen proven parent-key corrections exist only
in staged copies; nine notes without a proven parent remain explicit exceptions.
Archived note versions are outside this current-note snapshot.

PDF input preflight passed for **2,511 sources: 2,030 mains and 481 SI**, producing
161,225 canonical chunks. The declarations preserve exact parent/attachment
identities, source hashes and main/SI roles. All 16 frozen diagnostic PDF hashes
are included. Coordinate and neighbor validation passed before embedding.
Extraction, vector generation and serving acceptance are separate gates.

The old indexed-source inventory contains 2,332 parent/path rows, including
duplicate file representations. This is a different unit from the 2,320 distinct
paths reported in [Module 3](FULL_LIBRARY_VALIDATION.md).

| Final disposition of legacy rows | Count |
|---|---:|
| Same parent and declared path in the candidate | 2,064 |
| Same parent and current-inventory SHA-256 at a relocated path | 8 |
| Earlier unresolved exceptions, retained in legacy | 244 |
| Newly explicit held rows, retained in legacy | 16 |
| **Total** | **2,332** |

The 16 additional held rows correspond to existing source decisions: 12 rows
across nine SI-only parents with no evidence-backed main; three rows from a
parent with an unresolved same-attachment main/SI conflict; and one damaged main
PDF with unexpected EOF and no same-parent/same-hash replacement. All **260
legacy exceptions** remain accessible in the retained old library. No attachment
identity or role was guessed to increase coverage.

Four additional main PDFs produced no canonical text and are explicitly excluded;
these are outside the old 2,332-row inventory. Another source that initially
failed extraction succeeded on a later real preflight attempt, producing 3,968
chunks, and is included. Failure history is retained without labeling a recovered
source as still missing.

Same-path and current-inventory hash matches do not certify the bytes used to
build the historical legacy index. The new source manifest establishes the
candidate's current identities; it does not retroactively establish legacy
provenance. Private source-level manifests, hashes and exception evidence remain
local rather than publishing personal filenames or paper contents.

## Build and recovery behavior

The embedding model remains local `qwen3-embedding:4b`, 2,560 dimensions, digest
`df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907`.
Build and serving use the same measured IPv4 loopback endpoint. Chroma is 1.5.5.

Bounded batches reduce local HTTP overhead while preserving the embedding
contract. A batch is fully validated before any successful input is recorded.
Explicit transient runner failures and correctly sized all-zero/nonfinite
vectors share a maximum of three attempts with model identity rechecked each
time. Persistent or non-retryable errors stop publication. Receipts distinguish
successful input count from HTTP attempt count and record each recovery class.

Earlier failed candidates are retained. A real full build exposed both a runner
connection failure and an invalid-vector failure; the latter's original vector
was not captured, so its exact defect is unknown. The successful instrumented
notes run captured an all-zero vector and recovered without recording it as a
successful input.

The notes generation `0a0aade879fc42709101439700c69bc8` published 41,233 sections
from 2,143 notes in 6,261.9 seconds. Its receipt records 2,581 embedding HTTP
attempts, including two transient-connection retries and one invalid-vector
retry, with no cleanup warnings. The successful input count is 41,233; the
2,578 successful batches plus three retries account for all HTTP attempts.
Configured-client acceptance remains a later gate.

See [deployment and recovery](LOCAL_DEPLOYMENT.md) and
[embedding build sessions](development/EMBEDDING_BUILD_SESSIONS.md).

## Release gates

- Source inventory, exception accounting and final PDF preflight: passed.
- Local regression: 485 passed, 3 skipped. All 12 GitHub checks passed for the
  implementation commit `8af6273`.
- Notes candidate generation and build receipt: published. Complete PDF
  generation and joint reader validation: pending.
- Real MCP, 30 frozen W6 questions repeated three times, source-coordinate
  coverage, all six tools and citation/error cases: pending.
- Five actual client answer flows with independent semantic support review:
  pending. Follow-up retrieval and its filters/budgets must be reported.
- Accepted runtime selection, fresh persisted-client sessions and observed
  actual client tool discovery: pending.
- Final PR merge, local/remote primary-branch synchronization and released-code
  client smoke: pending.

The old snapshot has passed a real MCP readiness check: 150,244 PDF chunks and
2,140 legacy note records. It remains a usable fallback with explicitly
unverified legacy provenance. Its readiness is not canonical answer acceptance.

The known W6 diagnostic set is not a held-out benchmark. Its eight negative
controls were defined on eight papers and may be answerable in a larger library.
Earlier full-library legacy coordinate recall was unscorable; no score gain can
be claimed against it. Citation integrity also does not establish scientific
support. Final measurements will state their corpus, model, source budget,
top-k, labels and aggregation.
