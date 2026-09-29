# Incremental Note Contract

`multifacet-spec` must align with the current `$LOCALRAG_NOTES_DIR` candidate-first workflow. Gemini-generated notes are not allowed to write final human-confirmed tags.

## Required Frontmatter Fields

Generated notes should contain:

- bibliographic fields
- `combined_hash`
- `zotero_parent_key` when available
- `pdf_0_*` / `pdf_1_*`
- `research_domain`
- `document_type`
- `note_template`
- `seed_terms`
- `scope_hint`
- `signal_quality`
- `tags: []`
- `candidate_tags_high: []`
- `candidate_tags_medium: []`
- `candidate_tags_low: []`
- `human_reviewed: 0`

## Fields That Must Stay Out Of Final Note Frontmatter

These belong only in run artifacts:

- `tag_review_status`
- `candidate_needed`
- `candidate_needed_raw_terms`
- `routing_evidence`
- `warnings`
- `body_evidence_targets`

## Alignment Rules

- `seed_terms` are the highest-priority deterministic evidence source
- `scope_hint = other` should collapse downstream to `application/other`
- `scope_hint = needs-body-evidence` marks a note for the v1.5 Chinese body-evidence layer
- `signal_quality = weak` should reduce topic-only evidence weight
- Gemini may suggest candidate evidence, but it must not decide final `tags`
- Candidate Tagger is advisory only and must not directly write candidate layers into the final note unless the workflow explicitly does so
