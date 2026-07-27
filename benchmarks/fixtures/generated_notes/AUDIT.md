# S5 generated-note cross-audit

These fixtures are model-generated benchmark candidates, not gold notes.
Independent subagents checked candidates they did not draft against every
main-PDF and SI-PDF physical page. All generation, repair, and audit work used
the repository-local `skills/gemini-literature-processor` contract; no
machine-global skill or external search supplied note content.

## Final gate

| Paper | Template scope | Final audit | Residual candidate defects |
|---|---|---|---|
| `liu-2024-single-atom-cobalt-orr` | active-domain | eligible for human review | Source-set conflict retained: `95.2` vs `92.2 mA cm^-2` |
| `papier-2024-proteomic-cancer-risk` | field-neutral | PASS: P0/P1/P2/P3 = 0/0/0/0 | None; unexplained source denominators remain explicit |
| `cornelio-2023-ai-descartes` | field-neutral | PASS: P0/P1/P2/P3 = 0/0/0/0 | None |
| `dorgeist-2024-terrestrial-carbon-fluxes` | field-neutral | PASS: P0/P1/P2/P3 = 0/0/0/0 | None |
| `smith-2024-supply-chain-regulations` | field-neutral | PASS: P0/P1/P2/P3 = 0/0/0/1 | Editorial only: a heading says “three” while two concerns survive |

`PASS` means no P0 or P1 defect remained after repair and re-audit. It does
not replace the human source verification required for promotion into
`benchmarks/gold/notes/`.

## What the audit changed

The first cross-audit found recurring problems that were more general than any
single paper:

- cross-domain papers could be routed into an in-pack methods or theory
  template based on document shape alone;
- participant, sample, response, association, and entity counts could be
  silently merged or retyped;
- printed SI labels could be emitted in place of numeric physical PDF pages;
- timeout, explicit failure, counterexample, unknown, automatic proof, bounded
  distance, and manual proof could be collapsed;
- reviewer severity could target a stronger causal or general claim than the
  bounded claim actually written;
- a proposed “decisive” test could fail to identify one of its stated
  alternatives.

The repository contract now guards each failure mode. A source-verifiable P1
found after Stage B triggers a narrow `note_repair` manifest for a fresh
subagent. The original candidate is quarantined locally, the repaired JSON is
schema-validated and rendered through the scanner, and a different subagent
repeats the source audit. The committed fixture manifest records repair model,
prompt, brief, and pre-repair candidate fingerprints.

## Candidate-specific verification

### `papier-2024-proteomic-cancer-risk`

- Preserves `54,306`, `54,221`, and `44,645` as distinct participant-flow
  denominators and does not invent an explanation for the 85-person difference.
- Distinguishes the `337,543` exome projection ceiling from the `336,823`
  maximum genetic-analysis sample.
- Keeps `304` and `83` typed as protein-cancer association counts.
- States that nine associated proteins are targets of drugs indicated for the
  corresponding cancer; it does not rewrite this as nine drugs.
- Records the three TNFRSF14 genetic estimates as odds ratios.
- Judges the bounded association and prioritization claims at their stated
  strength rather than penalizing them for not proving causality.

### `cornelio-2023-ai-descartes`

- Separately records automatic `Yes`, explicit `No`, the demonstrated
  counterexample, timeout, bounded-distance results, manual parameter
  instantiation, and the absence of reported `unknown/not tested` instances.
- Keeps `g5`/`g7` as manual closure after automatic timeout.
- Reports that five selected full-pipeline tasks succeeded while leaving
  selection criteria, timing, blinding, and exclusions as unreported; it does
  not infer selection on prior success.
- Retains the unfiltered, equal-budget end-to-end evaluation gap as the one
  surviving major scientific concern.

### `dorgeist-2024-terrestrial-carbon-fluxes`

- Maps all five BLUE settings to their scenario roles and main flux-difference
  equations.
- Separates model min-max ranges, standard deviations, external estimate
  ranges, input sensitivity, and structural uncertainty.
- Treats `1.4` versus `0.2 GtC yr^-1` as a methodologically non-equivalent
  cross-study discrepancy, not an internal source conflict.
- Does not assign local-representation effects specifically to the 20-year
  smoothing step without source evidence.
- Uses a two-factor carbon-density × land-use-forcing design when claiming to
  discriminate the two input branches, and does not use net-flux agreement to
  validate unobservable components.

### `smith-2024-supply-chain-regulations`

- Separates respondent counts (`6,000` in the main paper; `6,001` in SI),
  proposal responses (`20,000/20,000/20,010`), and the source-labelled
  analytical sample/observations (`1,761/1,615/1,723`).
- Uses numeric SI physical-page citations only.
- Keeps online-sample support and cross-study similarity descriptive, while
  preserving the stronger mechanism and null-effect concerns where the source
  warrants them.
- Avoids statistical-significance wording for an untested cross-country
  difference.

## Remaining product limitation

Dedicated-template coverage is still only 1/5. The four field-neutral generic
notes are now valid candidates, but the benchmark should use human review to
decide whether epidemiology/proteomics, software/formal reasoning,
earth-system modelling, or survey/conjoint templates measurably improve note
quality and retrieval. The next gate is human source review followed by the
25-query S5 annotation and qrel pass; generic candidates must not be promoted
to gold by copying them unchanged.
