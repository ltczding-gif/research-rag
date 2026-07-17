# Domain pack template

This is a starter skeleton for adapting the literature-note pipeline to a new
research field. It ships verbatim copies of the catalysis pack's universal
parts (review / dissertation / theory / generic templates) plus placeholder
stubs for the field-specific parts.

## Don't edit this directory directly

Run the bootstrap CLI from the repo root:

```bash
python scanner/bootstrap_domain_pack.py --name <your-field>
```

The script copies this skeleton to `domain-packs/<your-field>/`, asks 5-6
questions, and customizes the output files. After it runs, the new pack
directory is yours to refine.

## What's in this skeleton

| File | Universal? | What you do with it |
|---|---|---|
| `pack.yaml` | needs your fields | Bootstrap fills in name, description, primary_routing_key |
| `prompts/document_profiler.system.txt` | yes | Leave as-is (universal) |
| `prompts/note_generator.system.txt` | yes | Leave as-is (universal) |
| `prompts/seed_terms_guidance.txt` | **field-specific** | Replace catalysis-style examples with your field's vocabulary |
| `prompts/routing_disambiguation_hints.txt` | **field-specific** | Add tie-breakers your documents trigger |
| `schemas/document_profile.vertex.schema.json` | **field-specific enums** | Replace `research_domain` and `recommended_template` enum values |
| `schemas/structured_note.vertex.schema.json` | mostly universal | Update `research_domain` enum to match the profile schema |
| `templates/_domain_quality_rules.txt` | **field-specific** | Define your field's trap-scan checklist + filename slot semantics + scoring axis-4 name |
| `templates/research-article.txt` | **field-specific** | Write your primary experimental-paper template body structure |
| `templates/review-or-perspective.txt` | yes | Keep |
| `templates/phd-dissertation.txt` | yes | Keep |
| `templates/foundational-theory.txt` | yes | Keep |
| `templates/generic-research-note.txt` | yes | Keep (fallback for documents that don't fit elsewhere) |
| `config/model_routing_policy.json` | mostly universal | Tune page-count thresholds if your field's papers run unusually long/short |

## Authoring order (recommended)

1. **Schemas first**. Decide your `research_domain` enum values, your
   `recommended_template` list, and the `primary_routing_key` field
   semantics. These commitments propagate everywhere.
2. **`_domain_quality_rules.txt`**. Define trap scan checklist (5-10 items),
   filename slot semantics, scoring axis-4 name. This is what makes your
   pack feel like *your* field's discipline.
3. **One template** — usually `research-article.txt`. Write the body
   structure for your most common paper type. Resist writing all templates
   at once.
4. **Dry-run validation**. Pick 5 PDFs from your library (one per
   `recommended_template` enum value). Run the scanner with `--limit 5`.
   Eyeball the output: is the routing right? Are seed_terms grounded?
5. **Iterate** on the template based on the dry run. Then write the
   remaining domain-specific templates with the lessons learned.
6. **Write `seed_terms_guidance.txt`** AFTER you've seen 5 real outputs —
   the calibration is much better with concrete examples.

## What the universal layer guarantees

`prompts/_universal_rules.txt` (at repo root, NOT in this pack) defines:
- Evidence citation contract (`[p.X]`, `value + unit + condition`)
- Anti-hallucination guardrails (`原文未提及` marker)
- Depth citation block (mechanism / observation / interpretation pattern)
- Subjective scoring four-axis pattern (originality / rigor / evidence
  closure / your-domain-specific-axis)

You don't need to re-implement these. Your pack's `_domain_quality_rules.txt`
extends them with field-specific extras.

## Reference: how catalysis filled in this skeleton

See `domain-packs/catalysis/` for a complete worked example. The directory
layout there is identical to this template; the file contents are filled in
with electrochemistry-specific details. Diff `_template/` against
`catalysis/` to see exactly what a real pack adds.
