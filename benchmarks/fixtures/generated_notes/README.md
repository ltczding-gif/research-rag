# Frozen generated-note fixtures

These five files are the reproducible S5 **candidate outputs**, not reference
notes and not gold data. Each candidate was produced through the repository's
two-stage subagent protocol from the paper's main PDF and SI PDF, rendered by
the normal scanner adapter, and required to pass canary validation before it
was frozen here. When an independent cross-audit found a source-verifiable P1
defect, a fresh subagent applied a narrow manifest-driven repair to the
candidate JSON before it was rendered and audited again.

`manifest.jsonl` records the backend, model, domain pack, routed template,
generation time, run id, prompt fingerprint, candidate JSON checksum, rendered
note checksum, repair provenance when applicable, and review status. Local
absolute PDF paths are replaced with benchmark-relative artifact paths before
commit.

The fixtures intentionally remain unchanged when a human later edits the
corresponding reference note. That preserves the generated-note baseline for
the `Generated-note E2E` track. Human-reviewed revisions belong in
`benchmarks/gold/notes/`.

## S5 template coverage

| Paper domain | Routed template | Current interpretation |
|---|---|---|
| Catalysis/materials | `electrocatalysis-experimental` | Dedicated template available |
| Biomedicine | `generic-research-note` | Missing epidemiology/proteomics contract |
| CS/ML | `generic-research-note` | Missing software/ML and neuro-symbolic methods contract |
| Environment/geoscience | `generic-research-note` | Missing environmental-model and carbon-accounting contract |
| Social science/economics | `generic-research-note` | Missing survey/conjoint and policy-inference contract |

This 1/5 dedicated-template coverage is a measured S5 limitation. It should not
be hidden by treating the generic template as a successful domain-specific
route. The four missing template families are candidates for later domain-pack
iterations, after S5 human review identifies which sections and scoring axes
actually improve retrieval and note quality.

## Field-neutral regeneration and promotion gate

The four non-catalysis fixtures were regenerated after the generic contract
was made field-neutral. Their Stage B manifests exclude catalysis-specific seed
guidance, naming semantics, and quality axes; their records therefore use
`rule_scope: field-neutral`. The catalysis candidate continues to use its
dedicated active-domain template.

All five records are `eligible-for-human-review`, but none is gold. This status
means only that the candidate passed the repository protocol, canary checks,
and independent P0/P1 cross-audit. A person must still verify the five source
sets before corrected copies can enter `benchmarks/gold/notes/`.

Cross-document citations use numeric physical PDF pages:
`[Main p.X]` for the article and `[SI p.X]` for supplementary information.
Printed labels such as `S1` may be described in prose but cannot replace the
physical page number in the citation token.

## Maintainer refresh

After completing five validated Wave 0B2 local subagent runs:

```bash
python benchmarks/scripts/freeze_generated_notes.py
```

The command is a maintainer utility. Normal CI consumes the committed fixtures
and does not invoke a model or subagent.
