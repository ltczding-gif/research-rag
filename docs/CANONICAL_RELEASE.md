# Canonical library release — Modules 4–5

Status: **canonical local runtime selected for research assistance**, 2026-09-18.
The source snapshot is dated 2026-09-17. Source migration, service integration
and persisted-configuration checks are complete. Answer review found four
complete answers and one reviewer-corrected partial answer; the client run did
not pass its strict follow-up-count protocol. These limitations remain part of
the release, not a claim of unattended scientific reliability. Code release and
CI are tracked in [PR #17](https://github.com/ltczding-gif/research-rag/pull/17).

## Reconciled candidate inputs

This is a complete build of the reconciled input snapshot, not a certification
that every Zotero attachment is represented. Held and undeclared sources remain
outside this candidate and are not silently included in its coverage totals.

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
The same generation was subsequently exercised through the configured client.

The PDF build stored all 161,225 inputs, then failed during a single oversized
SQLite ID-verification query. Verification now uses bounded groups of 500 IDs;
regression coverage includes a missing ID in the final partial group. Recovery
preserves the sealed failed generation and copies verified stored vectors into
a new generation, checking the fresh source/page plan and every source and
destination record. The original PDF embedding-session receipt was not written;
its HTTP/retry counters and adapter vector digest are unavailable. A separate
recovery receipt records storage-level numerical equivalence without inventing
those missing measurements. A first copy was retained as failed because Chroma
rewriting introduced small float32 differences and the bitwise check rejected
it. The subsequent copy compares every source/destination vector with explicit
limits: maximum coordinate difference 1e-7 and L2 difference 1e-6, retaining
both vector digests and measured errors. It does not claim bitwise identity.

The recovered PDF generation `ae58ebde4e7b4e798c86d777873c0a46` is complete:
2,511 sources, 50,784 page records and 161,225 chunks. Fresh sources, pages,
documents, metadata and all IDs matched; every destination vector was read back.
22,330 vectors changed at the float32 level, with maximum coordinate difference
7.450580596923828e-9 and maximum L2 difference 6.840389306575984e-8. Both are below
the declared limits. Recovery made no new embedding requests. Both failed
generations remain sealed and retained. The configured-client results below
use this recovered generation.

See [deployment and recovery](LOCAL_DEPLOYMENT.md) and
[embedding build sessions](development/EMBEDDING_BUILD_SESSIONS.md).

## Real MCP diagnostic results

The accepted candidate passed all six tools through the actual stdio service:
note discovery and full-note retrieval, verified PDF source windows, bounded
answer packets, valid citations, rejection of a forged quotation, and an empty
answer when a deliberately nonexistent source filter returns no evidence.

The frozen W6-v2 suite used the complete candidate, unchanged local model,
30 questions repeated three times, top 10, no source filters, and no expanded
neighbor context for the retrieval metric. All 31 labeled evidence spans were
coordinate-scorable. The first repetition covered **17/31 spans (54.84%)**, a
micro-average requiring complete gold-interval coverage with matching file and
page hashes. This is evidence coverage, not answer accuracy or an improvement
over the legacy run. The legacy coordinate metric was unscorable.

Across 90 sequential requests, median wall time was 0.136 seconds, nearest-rank
p95 0.215 seconds, and maximum 10.333 seconds (the first query). Process
initialization took 10.386 seconds. Top-10 membership was identical across all
three repetitions for 22/30 questions; exact order was identical for 12/30.
These are local diagnostic measurements, not a concurrent-load benchmark.

The missing first-pass evidence makes source discovery and focused follow-up
retrieval necessary for some questions. Citation integrity does not establish
that an answer's scientific conclusion follows from its sources.

## Actual client answer review and runtime selection

A fresh native Codex CLI 0.149.1 client, using `gpt-5.6-terra` with high reasoning,
completed 45 real MCP calls without transport errors. It received the five
known questions and workflow instructions, but no gold labels or source keys.
Source keys were discovered through note search and full-note retrieval. All
nine citation checks used unchanged server packets. Two initially rejected
quotation bindings were repaired in the client run.

Each question started with an unfiltered top-10, 8,000-codepoint request. Final
evidence remained within 8,000 codepoints per question; the two cross-paper
answers used two 4,000-codepoint packets checked separately. Source discovery
and earlier superseded packets are additional retrieval work, not part of the
final evidence budget.

| Question | Independent semantic review | Focused follow-ups |
|---|---|---:|
| W6-L03, PtBi ORR activity | Complete; measurement and comparator supported | 2 |
| W6-L04, Pt/Cu onset interpretation | Partial; corrected to generic CO-kinetic-model context | 2 |
| W6-L10, acidic PtBi MOR | Complete; conditions, units and durability supported | 1 |
| W6-C05, two OER durability studies | Complete; separate source conditions preserved | 3 |
| W6-C06, two structural strategies | Complete; source-specific conclusions supported | 3 |

The L04 client answer extended beyond its cited evidence. Host review replaced
it with a narrower statement about potential-dependent activation energies and
H–CO transition-state/adsorption-energy relationships. A further actual MCP
check passed against the unchanged saved packet, and independent review
confirmed the correction. This is a reviewer-corrected **partial answer**, not
an independently successful client answer. The direct Pt(111)/Cu(111) numerical
onset comparison and Pt HER-versus-CO2RR evidence remain missing.

C05 and C06 each used three focused follow-ups instead of the permitted two.
Consequently this run is **not a strict protocol pass**, five-of-five semantic
completion, or a held-out accuracy measurement. It supports local assisted use
with explicit evidence gaps and semantic review. Follow-up efficiency and
first-pass retrieval coverage remain improvement work.

The project's environment and MCP configuration now select the two accepted
canonical generations, the original full-note source directory and the same
local embedding model. The prior configuration is backed up byte-for-byte, and
the previous absence of a project environment file is recorded. The global
client configuration and original source data were not changed.

Full-library startup took about ten seconds. An initial optional-server client
run exposed no tools; project-scoped `required = true` made native client startup
wait for the server. Two fresh SDK sessions using the exact persisted entry
then passed tool discovery, readiness, note search, evidence retrieval and
citation checks with identical accepted generation IDs. SDK transport and native
tool discovery are separate checks. Existing desktop connections need a
reconnect to adopt the new configuration; they are not claimed to have refreshed
automatically.

## Release gates

- Source inventory, exception accounting and final PDF preflight: passed.
- Local regression: 487 passed, 3 skipped. All 12 GitHub checks passed for code
  commit `c3b49c9`; PR #17 also records checks on the final documentation commit.
- Notes and PDF candidate generations: published. The PDF recovery receipt
  records the missing original session measurements and numerical-copy boundary.
  Joint reader and MCP validation: passed.
- Real MCP, 30 frozen W6 questions repeated three times, source-coordinate
  coverage, all six tools and citation/error cases: passed as reported above.
- Five actual client answer flows: reviewed with the partial-answer and
  follow-up-limit exceptions above; strict protocol acceptance was not achieved.
- Local runtime selection and two fresh persisted SDK sessions: passed. Native
  candidate tool discovery and answer workflow: observed and reviewed.
- Release closeout requires green checks on the final PR head, merge, main
  synchronization and a further native client smoke using the persisted entry
  on released code. PR #17 records the merge; the local release receipt records
  the final main commit and observed MCP response.

The old snapshot has passed a real MCP readiness check: 150,244 PDF chunks and
2,140 legacy note records. It remains a usable fallback with explicitly
unverified legacy provenance. Its readiness is not canonical answer acceptance.

The known W6 diagnostic set is not a held-out benchmark. Its eight negative
controls were defined on eight papers and may be answerable in a larger library.
Earlier full-library legacy coordinate recall was unscorable; no score gain can
be claimed against it. Citation integrity also does not establish scientific
support. The measurements above state their corpus, model, source budget,
top-k, labels and aggregation; they do not establish general answer accuracy.
