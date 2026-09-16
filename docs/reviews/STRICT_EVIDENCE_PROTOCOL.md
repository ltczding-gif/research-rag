# ResearchQA strict evidence protocol v1

Protocol ID: `researchqa-strict-evidence-v1`. Results produced under the old
chunk-ID overlap rule are historical `loose_hit_*` diagnostics and are not
comparable to strict scores under the same metric name.

## Gold contract

Each question contains required groups (AND). A group contains alternatives
(OR). Every verified alternative has state `exact` or `adjudicated`, a
non-empty `gold_version`, and one or more stable evidence spans. All spans in
one alternative must be covered. `weak_hint` and `unmapped` alternatives do
not enter the evaluable set or mapping-coverage gate.

A span is anchored to canonical source data, independent of chunk boundaries:

- `file_id` and SHA-256 `file_hash`;
- zero-based physical `pdf_page_index` and canonical `page_text_hash`;
- half-open `char_start_in_normalized_page` and
  `char_end_in_normalized_page`;
- exact `evidence_text` and its UTF-8 SHA-256 `evidence_text_hash`.

Automatic `exact` means a unique NFKC/alphanumeric text alignment. It proves
alignment, not scientific sufficiency. Repeated exact text is left as
`weak_hint` until adjudicated. Numeric signs, decimals, exponents, comparison
operators, percentages, degrees, and negation markers are preserved and must
agree; token-substring matches are rejected. Adjacent signs and suffixes are
included in the stored span, so `5` cannot verify source `-5`, `1.2` cannot
verify `12`, and a true `42%` span retains `%`. Page, section, and fuzzy
matches only propose weak candidates; they never become verified gold by
lowering a threshold.

## Adjudication sidecar

The loader accepts this versioned JSON form. Each span is checked against the
current `CanonicalDocument`; mismatched identity, hashes, bounds, quote text,
unknown alternative IDs, or invented spans fail closed.

```json
{
  "schema_version": 1,
  "protocol_version": "researchqa-strict-evidence-v1",
  "adjudications": {
    "ea-...": {
      "verification_state": "adjudicated",
      "gold_version": "audit-2026-09-17-v1",
      "provenance": {
        "label": "agent_adjudicated",
        "source": "gold-audit.json",
        "source_revision": "sha-or-version"
      },
      "spans": [
        {
          "file_id": "Main",
          "file_hash": "<sha256>",
          "pdf_page_index": 0,
          "char_start_in_normalized_page": 120,
          "char_end_in_normalized_page": 180,
          "page_text_hash": "<sha256>",
          "evidence_text": "exact canonical substring",
          "evidence_text_hash": "<sha256>"
        }
      ]
    }
  }
}
```

`evidence_alternative_id(row_id, group_index, alternative_index)` produces the
sidecar key. `load_gold_adjudications(path)` loads the envelope, and
`map_all_references(..., gold_adjudications=sidecar)` performs canonical
validation. `item_source_spans_from_chunks(chunks)` rebuilds the scorer input
from a frozen IR/chunker without rerunning embedding or reranking models.

## Strict scoring

At each rank, the scorer unions retrieved source intervals with identical
file hash, file ID, page index, and page hash. An alternative completes only
when the union fully covers every one of its spans. The group's completion
rank is its earliest completed alternative; question success requires every
group. A one-character overlap therefore remains a loose hit and receives no
strict evidence credit. Missing retrieved-span provenance blocks strict
scoring instead of falling back to the old chunk-ID rule.

The primary names `recall_at_*`, `mrr`, `coverage_ndcg_at_10`, and
`all_required_groups_success_at_*` now use this strict rule and every mapping,
question result, and candidate result carries the protocol ID. The old rule is
reported only under `loose_hit_*` names. Questions with no verified reference
remain non-evaluable with null retrieval metrics. Candidate comparisons still
require an identical evaluable set.

For `G` verified groups, let `r_g` be the first rank at which group `g`
completes. Recall@K is `count(r_g <= K) / G`; MRR is
`1 / min(r_g)` (zero if none completes). Coverage nDCG@10 is
`sum(1 / log2(r_g + 1), r_g <= 10) / G`: each newly completed group gives
one unit of gain, and the ideal is all `G` groups completed at rank 1.
Repeated coverage gives no additional gain. All-groups success@K is one
only when all `G` groups complete by K. Scores aggregate as question means
within paper, paper means within domain, then equally weighted domain means.

The offline review report additionally publishes a **full expected-group
lower bound**: unresolved groups remain in the denominator and receive zero
verified credit. Conditional scores use only the shared verified set and
are labelled separately. A question with references but no verified group
therefore has zero in the full lower bound and null conditional metrics;
a question with no references has null retrieval metrics in both. This
lower bound does not assert that an unresolved group's evidence was absent
from the retrieved context. Mapping coverage and all unresolved groups
remain visible; the report never turns incomplete gold into a release win.

For the fixed-context comparison, use an 8,000 Unicode-code-point ceiling.
Consume whole ranked chunks in order and stop before the first chunk that
would exceed the ceiling; do not skip or truncate it. Report actual characters
and chunk count for every candidate.

## Historical rescore inputs

The rescore CLI requires `--historical-bindings` in addition to the source
run, questions, candidate files and optional adjudication sidecar. This is a
separate, previously verified JSON manifest with schema version 1, the exact
question-file SHA-256, and candidate records keyed by config ID. Each record
contains the artifact SHA-256, payload SHA-256, original input fingerprint,
engine revision, and full candidate definition. The CLI checks all these
identities and reconstructs every ranked chunk's source coordinates before
scoring. It never creates trusted bindings from the candidate being checked.

Establish bindings against a frozen external run manifest or reproduce the
historical input-fingerprint algorithm using the original config, questions,
canonical sources and model identity. Record that provenance in the binding
manifest. Merely recomputing a candidate's self-reported payload hash cannot
authenticate its query text or claimed retrieval route. The output records
the binding-manifest hash and scoring implementation snapshot; existing
output paths are refused.
