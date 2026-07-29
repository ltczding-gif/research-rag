# ResearchQA rq-2 strategy report

- Frozen matrix candidates: 35
- Frozen completed / failed: 34 / 1
- Frozen validity classes: 6 valid-and-rankable, 26 valid-but-poor, 2 diagnostic-only/ineligible, 1 deterministic-strategy-failure, 0 infrastructure/unknown, 0 invalid-false-score
- Approved extensions: 4 (2 valid-and-rankable, 2 valid-but-poor)
- Provisional winner: `repair-rr1-093b3f922f8306f447ae`
- Winner coverage-nDCG@10: 0.848098
- Winner p95 latency: 1331.153 ms (observed-only)
- Paired delta vs fixed hybrid PDF-only baseline `reranker-4c0466f31afde6284ef0`: +0.013572 (95% CI +0.008793 to +0.018389; 10,000 domain-stratified paper resamples)

The original outer runtime task retains its historical CUDA failure. It was not rewritten. Publication is opened by the hash-bound superseding reconciliation after the 35-candidate validity audit, note pre-quality gate, and all four approved extensions reached auditable terminal states.

No sustained thermal observation record was supplied; interpret latency together with the hashed hardware identity.

The PDF chunking arm retrieves only each paper's Main benchmark PDF. SI and auxiliary sources are mandatory inputs to generic-note generation, so note-based arms can contain SI-derived content; this run does not measure direct SI/native-source retrieval.

The winner is provisional and passed the rq-2 relative regression guardrails. Its latency is observed-only and is not a controlled production SLA. Operational ratios above 1.5x still require an explicit quality justification and rollback switch before production use.

Reconciliation status: completed; no invalid false score entered the final decision.

This run stops after rq-2 and does not start rq-5 automatically.
