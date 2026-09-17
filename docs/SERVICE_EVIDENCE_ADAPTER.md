# Service evidence fixture verification

`service_evidence_adapter.py` is a pure converter. It accepts an already
validated `EvidenceMapping`, an already-recorded canonical `search_papers`
response, captured request facts, the manifest SHA, and the raw canonical
`pages.jsonl` bytes. It calls the existing `RetrievedEvidenceSpan` and
`score_ranking`; it does not retrieve, embed, or recreate identity/gold logic.

The hard gates are: exact query/effective-query/filters, canonical mode,
one manifest generation and embedding contract, manifest-pages artifact SHA,
metadata/evidence/source-span agreement, canonical-page quote slicing and full
chunk reconstruction, finite rank/distance preservation, and paper-scope result
isolation. Corpus scope rejects paper/parent/attachment/file-name/ordinal
filters; supported role/type filters are applied again to every returned hit.

`ServiceRequest.from_tool_call()` derives query, second-query fallback, `n`, and
the exact AND filter expression from recorded tool-call arguments. It never
uses a response field as an expectation; unknown fields and `include_context`
are rejected. Recorded trace generation/model identity must equal the trusted
manifest, and result count cannot exceed `n`.

The trace runner requires a separately recorded `--manifest-sha256`; it does
not derive a trust value from the manifest it is checking. These checks establish
consistency with supplied material, not a signature or origin certification.

`AnswerContext` supports one exact serialization: the top-k rank prefix as
`[rank=N id=...]` plus returned content, joined by blank lines. It uses the
historical 8000 Unicode-code-point cap. If it was never captured, the emitted record is exactly
`answer_context.status = not_run`; ranking is still a retrieval-only score.
Likewise, an absent client total is `timings_seconds.client.status = not_run`.
For incomplete mappings it emits existing-scorer conditional metrics and a
separate full-required-group lower bound; it never drops unmapped groups.

The fixture verifier accepts only the fixed synthetic `main`, `si`, and `global`
trace set plus `synthetic-gold.json`. It is not a general scoring CLI. It opens
no Chroma database, PDFs, embedding model, or user library.

The fixture uses deliberate evidence-marker queries as functional controls.
The adapter does not bind queries or rewrites to a frozen benchmark question;
that remains the benchmark caller's responsibility. These controls therefore
cannot establish non-oracle retrieval quality. Each output source span retains
the already-verified quote and quote hash for direct inspection.

```powershell
C:\Users\Link\.localrag\venv\Scripts\python.exe -m pytest tests/test_service_evidence_adapter.py -q
C:\Users\Link\.localrag\venv\Scripts\python.exe benchmarks\scripts\verify_service_evidence_fixture.py path\to\fixture --manifest-sha256 d526f60fd9a1eff54c10acc17f95b90667ccb7e9e92799fe734a8cfad8a004ee --output path\to\fixture-score.json
```

The emitted fixture output is marked `synthetic_offline_service_trace` and
`eligible_for_release_claim = false`.
