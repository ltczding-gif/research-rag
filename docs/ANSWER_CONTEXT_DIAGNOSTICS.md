# Frozen-context answer diagnostics

This diagnostic asks the same local model the same question twice: once with
previously recorded retrieved evidence, and once with source-checked target
evidence. The second arm is an oracle control. It is deliberately selected with
knowledge of the required evidence and is not a retrieval-quality score.

The initial protocol is `benchmarks/protocols/w6-answer-rubric-v5.json`. The
project owner approved its five question-scope decisions on 2026-09-17. This
approval fixes the scoring policy; it does not turn agent evidence checks or
future answer reviews into human scientific adjudication. The five questions
have already been observed, so the run is a development diagnostic, not a
holdout. One sample per arm cannot establish a causal or statistical gain.

## Freeze inputs before generating answers

A local bundle uses schema `answer-context-diagnostic-bundle-v1` and contains:

- `protocol_path` and its raw-file `protocol_sha256`;
- the fixed model, digest, prompt, endpoint and generation settings;
- nonempty `source_bindings` entries with local paths and raw-file SHA-256;
- ordered `questions` matching the protocol's IDs and exact question text;
- `retrieved` and `oracle` evidence lists for every question, each using ordered
  `E1` through `En` citations, `content`, and nonempty `source_spans`.

Before accepting a bundle, the evidence reviewer must verify each source PDF,
canonical page, quote, offset, and reconstructed content. The runner checks the
frozen bundle, protocol, source-file hashes and prompt layout; a supplied hash
is not proof of scientific sufficiency. Source spans are retained for review,
not treated as an automatic answer verdict. The local W6 preparation also
binds the retrieved records to the hashes stored in the preserved v4 answers,
and verifies that the retrieved user prompts match v4 exactly.

Each context has an 8000 Unicode-code-point limit on evidence content; citation
headers are counted separately, matching the inherited experiment. The runner
rejects over-budget inputs instead of truncating them. It inserts only the
question and quoted evidence into the fixed prompt, never the rubric or an
authored reference answer. Oracle source passages may of course contain the
target facts.

## Run and inspect

Use an independently recorded bundle hash from the freeze receipt. Dry-run is
the default and produces the paired requests without contacting a model.

```powershell
$PYTHON = "C:\Users\Link\AppData\Local\Programs\Python\Python311\python.exe"
& $PYTHON benchmarks/scripts/run_answer_diagnostics.py --bundle <frozen-bundle.json> --bundle-sha256 <recorded-sha256> --output <new-dry-run-directory>
& $PYTHON benchmarks/scripts/run_answer_diagnostics.py --bundle <frozen-bundle.json> --bundle-sha256 <recorded-sha256> --output <new-run-directory> --execute
```

The live endpoint must be local Ollama. The runner copies the expected model
to a uniquely named run alias, verifies its digest, uses that alias for the
paired calls, and records cleanup of that exact alias. It does not change the
source model tag or automatically download a model. Identity checks establish
the recorded API contract; they are not cryptographic proof of internal model
weights for every token.

Outputs are written to a new directory. Inspect the manifest, paired requests,
raw responses, timing and identity receipts before reviewing answers. Failed
or missing outputs remain in the denominator. Transport, identity, generation
budget and incomplete-output failures are not evidence of retrieval failure.
The runner never automatically retries a failed generation or assigns a
scientific score.

## Review answers under one policy

Report core answer correctness, returned citation IDs, source locations,
claim-to-citation support, required evidence availability, optional condition
completeness, refusal decisions and unsupported claims after refusal separately.
Correct optional facts remain welcome; incorrect volunteered facts remain
errors. A historical answer may be reviewed under v5 in a separately labelled
record, but its original answer, v4 adjudication and scores stay unchanged.

If oracle succeeds where retrieved evidence fails, inspect whether evidence
was absent, obscured by irrelevant context, or cited incorrectly. Do not
automatically label every difference a retrieval defect. Failure with complete
oracle evidence supports further answer/citation diagnosis. Neither arm alone
licenses a general release-quality or cross-paper synthesis claim.

The [initial v5 results and next implementation plan](plans/2026-09-17-answer-diagnostics-v5-results.md)
record the ten local outputs' findings and remaining evaluation boundaries.
