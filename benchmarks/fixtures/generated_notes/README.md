# Frozen generated-note fixtures

These five files are the reproducible S5 **candidate outputs**, not reference
notes and not gold data. Each candidate was produced through the repository's
two-stage subagent protocol from the paper's main PDF and SI PDF, rendered by
the normal scanner adapter, and required to pass canary validation before it
was frozen here.

`manifest.jsonl` records the backend, model, domain pack, routed template,
generation time, run id, prompt fingerprint, candidate JSON checksum, rendered
note checksum, and review status. Local absolute PDF paths are replaced with
benchmark-relative artifact paths before commit.

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

## Known baseline defects and promotion gate

The current repository ships one production domain pack: `catalysis`. The four
non-catalysis candidates therefore inherit catalysis-specific seed guidance,
filename semantics, and the `工业应用潜力` scoring axis even though Stage A
correctly routes their note bodies to `generic-research-note`. This is a
domain-pack isolation defect, not acceptable cross-domain behavior.

The manifest consequently marks those four fixtures
`promotion_status: blocked-domain-pack-mismatch`. They remain useful as an
honest snapshot of the current system, but they must not be copied or lightly
edited into gold notes. First add the relevant domain contract, regenerate the
candidate, and then begin human review. The catalysis candidate is marked
`eligible-for-human-review`; it is still not gold until a person verifies it
against the main PDF and SI.

The current citation surface also mixes `[p.X]` for the main article with
`[SI p.X]` for supplementary information. A future cross-document citation
contract should normalize the first form to `[Main p.X]` before automated
source-level scoring.

## Maintainer refresh

After completing five validated local subagent runs:

```bash
python benchmarks/scripts/freeze_generated_notes.py
```

The command is a maintainer utility. Normal CI consumes the committed fixtures
and does not invoke a model or subagent.
