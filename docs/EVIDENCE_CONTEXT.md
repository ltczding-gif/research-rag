# Source-preserving evidence context

Canonical PDF search can return the original ranked hit together with a bounded
window from the same generation and attachment. The window is assembled from
canonical page coordinates, rather than concatenating overlapping chunk copies.
This helps retain subjects, measurement conditions and results split by chunk
or page boundaries without changing retrieval ranking or rebuilding an index.

## Calling and reading results

- The MCP `search_papers` tool defaults to `include_context=True`.
- HTTP and the shared Python function preserve their previous default of no
  expansion. Pass `include_context: true` explicitly to request it.
- Explicit `include_context=False` returns the original hits on every interface.
- `content`, `metadata`, `distance` and `evidence` describe the original hit.
- `context` contains the expanded text with `[MATCH]...[/MATCH]` around that hit.
- `context_source` describes the full expanded text: generation, file hash,
  parent/attachment/role, exact page spans, quotes and quote hashes. Its
  `for_chunk_id` points to the original hit; the expansion is not a new chunk.
- `match_start`/`match_end` are half-open offsets in the context **before** the
  two display markers are inserted. `text_hash` hashes that unmarked UTF-8 text.
- Canonical `context_source` replaces the previous neighbor-only
  `context_evidence` list. Consumers citing expanded text should use the new
  source spans. Legacy indexes keep their old, explicitly unverified neighbor
  concatenation and do not gain a verified `context_source`.

## Bounds and fidelity

The usual limit is 3200 Unicode code points per hit, excluding display markers.
The original hit is never shortened. If a configured index has a hit larger
than the limit, the entire hit is returned with `oversized_match=true` and no
extra surrounding text. This is a per-hit limit, not a global prompt budget.

The remaining budget is shared between preceding and following text; spare
space near a document edge is used on the other side. Within that window the
assembler prefers sentence boundaries and preserves PDF line breaks. Decimal
points and common `vs.`/`Fig.` abbreviations are not treated as sentence ends.
`boundary_status` separately reports `source_start`, `source_end`,
`sentence_heuristic` or `budget_cut` for the two edges.

Canonical source text is returned verbatim. There is no unit conversion,
superscript repair, caption removal, answer generation or inference that nearby
conditions belong to the same experiment. A PDF may place a caption between
parts of a sentence; the text and separate page spans remain visible. Sentence
boundaries are a display aid and do not establish scientific completeness.
Returned windows may overlap across separately ranked hits; the assembler
removes chunk-overlap duplication **within each context**, not across results.

Every included page's identity and text hash are checked, including pages added
outside the original hit. Expansion cannot cross into another paper, attachment
or MAIN/SI file. A missing or invalid required source page rejects the result
instead of fabricating an expansion. Neighbor chunk pointers are not needed:
the verified canonical source pages are the authority for this display.

## Module boundary

This module completes source-coordinate context assembly and its client-facing
contract. It does not retrieve a missing paper or distant passage, establish
whether a claim's citation is sufficient, or fix ambiguous PDF typography.
Those concerns remain retrieval, answer construction and source-review work.

For a complete host-driven answering flow with a total evidence budget,
cross-result deduplication and citation checks, use the
[answer workflow](ANSWER_WORKFLOW.md).
