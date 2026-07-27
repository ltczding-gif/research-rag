# S5 generated-note cross-audit

Three subagents cross-checked notes that they did not originally draft against
the corresponding main PDFs and SI PDFs. This audit qualifies the generated
baseline; it does not constitute the human sign-off required for gold notes.

## Promotion blockers

1. **Domain-pack isolation:** all five runs used the only production domain
   pack, `catalysis`. The four non-catalysis candidates therefore inherit
   catalysis-specific seed guidance, naming semantics, and the
   `工业应用潜力` score. Their manifest records are blocked from gold review
   until a matching domain contract exists and the notes are regenerated.
2. **Template coverage:** only the catalyst used a dedicated template. The
   biomedicine, CS/ML, environment, and social-science papers all used
   `generic-research-note`. Their domain-specific methods and validity checks
   are present only opportunistically in prose rather than as stable fields.
3. **Citation syntax:** main-paper citations use `[p.X]`, while supplementary
   citations use `[SI p.X]`. A later citation contract should require
   `[Main p.X]` and `[SI p.X]` for unambiguous automated scoring.

## Candidate-specific findings

### `liu-2024-single-atom-cobalt-orr`

- The kinetic current density at 0.85 V conflicts across the source set:
  the main article reports `95.2 mA cm^-2` on p.4, while SI Table S2 reports
  `92.2 mA cm^-2` on SI p.39. The candidate presents `95.2` as settled even
  when discussing Table S2. Gold claims and qrels must preserve this conflict
  rather than freezing one value silently.

### `papier-2024-proteomic-cancer-risk`

- The source gives two starting proteomics counts in different contexts:
  `54,306` on main p.8 and `54,221` measured EDTA plasma samples on main p.9.
  The final observational cohort of `44,645` is unaffected, but a sample-flow
  answer key must retain both starting counts and their meanings.
- A future epidemiology/proteomics template should explicitly capture cohort
  selection, participation bias, missing-data handling, proportional-hazards
  assumptions, genetic-instrument strength, pleiotropy, and sample overlap.

### `cornelio-2023-ai-descartes`

- A theorem-prover timeout is not proof that a formula is non-derivable. The
  candidate should distinguish `proved`, `disproved/counterexample`, and
  `not proved within the run limit`.
- Several benchmark and reproducibility judgments need explicit SI citations.
  A future software/ML template should structure task definition, algorithms,
  baselines, computational budget, ablations, proof status, and reproducibility.

### `dorgeist-2024-terrestrial-carbon-fluxes`

- No major factual or main/SI attribution error was found. A future
  earth-system template should structure simulation variants, equations,
  counterfactual differences, DGVM ensemble uncertainty, external flux
  constraints, and code availability instead of repeating them across generic
  prose sections.

### `smith-2024-supply-chain-regulations`

- No major factual or main/SI attribution error was found. A future empirical
  social-science template should distinguish respondents from proposal
  responses, absolute support from forced choice, randomization unit, estimand,
  clustered uncertainty, preregistration, exclusions, multiple comparisons,
  external validity, and null-result precision.

## Recommended next iteration

Fix domain-pack isolation first, then add the four missing domain contracts and
regenerate the blocked candidates. Only after that should a person review the
five source sets and promote corrected copies into `benchmarks/gold/notes/`.
