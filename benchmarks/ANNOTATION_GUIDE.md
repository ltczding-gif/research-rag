# Annotation guide v1

This guide is part of benchmark version `0.1.0`. Changes to judgment meaning,
evidence anchoring, or adjudication require a benchmark version change.

## Independence from candidate systems

- Gold evidence is anchored to the source PDF, never to a candidate `chunk_id`.
- Annotators use `pdf_page_index`, canonical page hash, an exact quote and hash,
  plus either canonical-page character offsets or a PDF bounding box.
- A candidate chunk is relevant only after the evaluator maps it to one or more
  stable `evidence_id` values.
- Multiple overlapping chunks that map to the same evidence can contribute gain
  only once.

## Relevance labels

| Label | Meaning |
|---:|---|
| `-1` | Unjudged. Never treat this as non-relevant. |
| `0` | Non-relevant or misleading. |
| `1` | Useful background but does not directly support the answer. |
| `2` | Supports part of the answer. |
| `3` | Core evidence or the best available hit. |

Document qrels use `paper_id`; evidence qrels use `evidence_id`.

## Answer keys and evidence groups

- Every query has one answer-key record, including negative queries.
- `expected_claim_ids` must match between the query and answer key.
- Every expected claim points to one or more stable evidence groups.
- For multi-hop queries, split independent requirements into distinct
  `required_evidence_group_ids`. Complete success requires every required
  group; partial evidence recall is reported separately.
- `no-answer`, `false-premise`, `ambiguous`, and `conflicting` are distinct
  answerability classes and must not be collapsed during analysis.

## Pooling

Discovery, cross-paper, and contradiction qrels are built from the union of
top-k results from dense, lexical, hybrid, reranker, and human candidate runs.
Keep unjudged targets at `-1`.

For the sealed H60 release run:

1. collect the baseline and candidate top-k union;
2. remove run identity and randomize presentation order;
3. complete missing judgments before metrics are calculated;
4. freeze the supplemented qrels;
5. mark the whole run `incomplete` if the pool cannot be judged.

Never drop a difficult query because `judged@k` is low.

## Review and adjudication

- Every release evidence unit receives a second-person review.
- At least 20% of release annotations are independently re-created blind.
- Report weighted kappa or Krippendorff alpha for 0-3 qrels.
- Report token-F1 for independently annotated evidence spans.
- Default acceptance thresholds are qrels agreement `>= 0.70` and span F1
  `>= 0.80`.
- If either threshold fails, revise the guide and re-annotate before producing a
  baseline. The project owner or a third annotator adjudicates disagreements.

## Privacy and licensing

- Official benchmark annotations use only redistribution-approved corpus files.
- Do not copy private vault paths, usernames, Zotero keys, query-log identity, or
  API credentials into annotations.
- `artifact_path` is repository-relative. Absolute paths and `..` traversal are
  invalid.
- Preserve required attribution and license metadata from the corpus manifest.
