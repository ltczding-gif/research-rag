# W6 v5 answer diagnostic: results and next plan

Date: 2026-09-17. Status: ten local generations completed; answer review is
agent review under the owner's approved policy, not human scientific adjudication.

## What was measured

Five previously observed development questions were each answered once with
the frozen v4 retrieved context and once with target-selected canonical source
excerpts (the oracle arm). The question, system prompt, model and generation
settings were fixed within each pair. No retrieval or reranking was rerun.
No failed or successful answer was retried or replaced. Historical v4 answers,
gold, adjudication and scores were preserved; they were not rescored in this run.

- Model: local `qwen3:4b-instruct`, Ollama 0.34.1.
- Model digest: `0edcdef34593eac1aa2be9c7d06c432dcf81945adca5eca2f27662c18f168ba0`.
- Temperature 0, seed 20260917, thinking disabled, context 16384, output budget 1200.
- Evidence budget: 8000 Unicode code points; citation headers counted separately.
- Frozen bundle SHA-256: `e1ef60ad9b4378d002036bc7398f11fb453499ba2a4ffab88c24aa0b6506ccb7`.
- Rubric raw-file SHA-256: `589d49ee3b1a2879b6b9115c4c38e1fb498463827e4e4820bac922356b22c1c7`.
- Source bindings: 39; retrieved user prompts exactly match the preserved v4 prompts.

All ten responses completed with `done_reason=stop`. API identity checks passed,
the owned temporary model alias was deleted, and the source model digest remained
unchanged. Execution success is separate from answer correctness.

## Findings

| Question | Frozen retrieved context | Retrieved answer | Oracle answer |
| --- | --- | --- | --- |
| L03 | Missing the explicit 0.9 V association | Appropriate insufficiency decision, followed by unsupported claims assigning MOR's 0.67 V to ORR | Voltage, value and 5.2-fold comparison present, but the answer adds a spurious `Pt^-2` factor to the activity unit |
| L04 | Required explanation available | Core explanation correct; the optional -0.6 V activation-energy claim cites passages other than the supporting E6 | Required explanation and volunteered onset values supported |
| L10 | Required activities and 4000 s outcome available | Core answer correct; extra 5000-cycle ORR evidence is presented as supporting acidic MOR durability | Required activities and durability outcome supported |
| C05 | Required outcomes and 1.50 V available across passages | Core answer correct; Co subject and RHE reference are outside the cited E6 fragment | Omits the required Co 1.50 V condition despite its presence in E2 |
| C06 | Structural strategies and durability available | Core comparison correct; cited E6/E7 do not establish the volunteered PtBi MOR claim, supported elsewhere in E9 | Required comparison supported; no unprovided Bi-dissolution mechanism asserted |

Strict core correctness is 4/5 for retrieved answers and 3/5 for oracle answers.
These counts deliberately do not imply that every additional factual claim has
valid support. L03 oracle fails the unit requirement, while C05 oracle fails a
required condition. L03 retrieved's appropriate refusal is reported separately
from its unsupported elaboration. Optional omitted facts do not fail core answers.

The L03 source typography needs care: visual inspection of the original page
shows Pt as the area label, while canonical extraction flattens the expression
to `mA/cm Pt-2`. The generated `mA/cm2 Pt^-2` is not a faithful unit rendering.
This case combines input typography ambiguity and answer fidelity; it must not
be attributed solely to missing model knowledge. The raw evidence and response
remain unchanged.

The -0.6 V activation-energy crossover and -0.85 V rate crossover in L04 are
different quantities. The former is supported in the retrieved context; its
problem in this answer is citation alignment, not a wrong numerical value.

This is a five-question, one-sample-per-arm development diagnostic. Target-aware
oracle selection changes evidence composition and length. These results are not
a retrieval score, a holdout, a causal estimate or evidence of general improvement.
They cannot be compared as a score gain against historical v4 results.

## Next implementation order and acceptance checks

1. **Preserve the condition and unit with the claim.** Build a small regression
   fixture from the observed L03 unit and MOR/ORR condition cases. Keep original
   source text and locations intact; any display normalization must be explicit
   and separately versioned. Verify that area subscripts do not become extra
   powers and that a caption from a different reaction cannot supply a missing
   measurement potential. Do not globally rewrite PDF units from this one case.
2. **Keep claim-to-citation support local and reaction-specific.** Use L04, L10,
   C05 and C06 as named development regressions. Preserve subject, condition and
   outcome when joining source spans. Verify the -0.6 V claim cites its actual
   passage, Co's condition includes the subject and reference when stated, and
   ORR cycling is not relabeled as MOR evidence. Valid E IDs alone are insufficient.
3. **Test concise, bounded answer construction.** In a new frozen experiment,
   require requested facts and conditions, and suppress unsupported elaboration
   after refusal. Keep this run as the baseline; version any prompt change and
   compare both arms under one protocol. Do not select the best of repeated runs.
4. **Only then consider a broader quality claim.** Freeze new, unobserved questions,
   source coverage, metrics, citation-support rules and human review requirements
   before evaluation. Existing W6 questions remain development cases. Human
   scientific adjudication and an independent acceptance set are outstanding;
   the owner's rubric approval is not a substitute for them.

This diagnosis does not justify a blanket Top-k increase, automatic v1 sidecar
migration, replacement of historical gold, or promotion of the research branch
to a release. Production data and the existing main/research separation remain
outside this iteration.

## Audit artifacts

Raw responses, prompts, source selectors, source-binding receipts, review cases
and detailed claim annotations remain in the local run archive. The run archive
is private because it contains paper excerpts. The public record intentionally
contains protocol, method, aggregate findings and implementation priorities only.
See [the diagnostic runner guide](../ANSWER_CONTEXT_DIAGNOSTICS.md) and
[v5 policy](../../benchmarks/protocols/w6-answer-rubric-v5.json).
