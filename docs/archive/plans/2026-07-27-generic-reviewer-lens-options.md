# Generic Reviewer Lens options

Decision: **Option C selected on 2026-07-27**. The active generic template now
implements the adaptive red-team verdict contract below.

The reviewer section must test whether the evidence can carry the paper's
load-bearing claims. It is not a second summary, a generic checklist, or a
reward for sounding harsh.

## Shared scientific floor

Every option must:

- reference concrete Claim IDs, Evidence IDs, and source pages;
- distinguish `fatal`, `major`, and `minor`;
- distinguish `supported`, `partially supported`, `unresolved`, `overstated`,
  and `contradicted`;
- identify the strongest plausible alternative explanation;
- name the single most decision-changing missing experiment, analysis, proof,
  control, or dataset;
- challenge only omissions that matter to the main claims;
- state the strongest contribution as well as the most fragile inference;
- avoid style, novelty theater, and boilerplate requests for "more data".

## Option A — Five-cut verdict

Output exactly five short items:

1. **Strongest claim:** the best-supported load-bearing claim and why it holds.
2. **Weakest link:** the assumption or evidence gap that most threatens the
   main conclusion.
3. **Best alternative:** the most plausible competing explanation.
4. **Decisive test:** the one addition most likely to change the verdict.
5. **Scope verdict:** the strongest conclusion the present evidence can safely
   support.

Strengths:

- shortest and sharpest;
- hard to turn into a second summary;
- good for fast reading and retrieval.

Risks:

- one concern may not represent a multi-claim paper;
- disagreements across several claims are compressed aggressively.

## Option B — Claim-level verdict table

Audit every load-bearing claim:

| Claim | Verdict | Evidence adequacy | Strongest alternative | Decisive missing evidence | Severity |
|---|---|---|---|---|---|
| C1 | supported / partial / unresolved / overstated / contradicted | E1 + pages | competing explanation | experiment / analysis / proof | fatal / major / minor |

After the table, output:

- the strongest contribution;
- the highest-severity concern;
- the maximum defensible scope of the conclusions.

Strengths:

- most auditable and directly connected to Evidence IDs;
- preserves disagreements between claims;
- maps naturally to benchmark claims and evidence units.

Risks:

- longer than Option A;
- can become mechanical if every minor claim is included;
- must be restricted to 3–7 load-bearing claims.

## Option C — Adaptive red-team verdict

Use the Claim-level verdict structure from Option B, but choose attack
questions internally according to the document's research design. Output only
the concerns that survive the attack; do not print the checklist.

Attack families:

- **Controlled experiment:** controls, measurement validity, randomization,
  blinding, intervention fidelity, multiplicity, mechanism closure.
- **Observational / clinical / social:** selection, confounding, measurement
  error, missingness, estimand, model specification, causal overreach,
  transportability.
- **Computational / ML / simulation:** leakage, baseline fairness, ablation,
  sensitivity, identifiability, compute budget, reproducibility, mismatch
  between benchmark and real task.
- **Theory / proof:** hidden assumptions, completeness, counterexamples,
  identifiability, equivalence of formal statement and claimed phenomenon,
  timeout versus non-derivability.
- **Review / policy / synthesis:** search coverage, inclusion bias, evidence
  weighting, unresolved disagreement, stakeholder assumptions, inference
  beyond the reviewed evidence.

Final output:

1. a verdict table for only the 3–7 load-bearing claims;
2. the top three surviving scientific concerns, severity-ranked;
3. one decisive test or analysis per `fatal` or `major` concern;
4. the maximum defensible conclusion and confidence boundary.

Strengths:

- strongest scientific precision across domains;
- avoids applying laboratory criteria to surveys, proofs, or simulations;
- keeps the visible output claim-centered while using domain-aware attacks.

Risks:

- most complex prompt contract;
- requires strong routing from research design to the right attack family;
- needs regression tests to ensure the hidden checklist does not leak into
  boilerplate output.

## Decision

Use **Option C** as the final contract, with two limits:

- no more than 3–7 load-bearing claims;
- no more than three surviving concerns in the final prose.

This retains Option B's auditability while preventing the reviewer section
from becoming a generic checklist or an unbounded list of possible criticisms.
