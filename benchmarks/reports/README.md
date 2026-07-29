# Public benchmark reports

This directory is reserved for sanitized, aggregate benchmark results that
can be reviewed without redistributing the benchmark corpus or source papers.

The only currently admitted report family is `researchqa-rq2/`. A completed
publication may contain:

- `morning-report.md`
- `leaderboard.csv`
- `paper-domain-breakdown.csv`
- `paired-bootstrap.json`
- `pareto-frontier.json`
- `reconciliation.json`
- `run-manifest.json`
- `blocked-and-unmapped.jsonl`

Before publication, every file must be generated from a completed, validated
run and checked for absolute paths, source passages, question or answer text,
private notes, credentials, and local runtime metadata.

`run-manifest.json` must be regenerated after the runtime task has completed.
The prepare-stage placeholder is not publishable. The final manifest must
record all 35 unique frozen-matrix candidates, the four approved extensions
(`F2`, `RR1`, `R1`, and `S1`), the evidence-mapping gate, the 10,000-sample
paired bootstrap, the Pareto frontier, the provisional winner, the
superseding reconciliation, and the code/config/model/data/hardware
fingerprints needed to reproduce the run.
The candidate stage counts are fixed at 7 PDF chunkers, 4 note chunkers,
3 retrievers, 5 source compositions, 4 reranker modes, and 16 top-two
Cartesian confirmation rows that reduce to 12 unique compatible combinations.
`hierarchical-pdf` requires `pdf-parent-child`, so varying the selected fixed
PDF chunker on that branch does not define a new strategy and must be
deduplicated before execution. Every unique candidate must have a terminal
status; an explicitly failed strategy is retained as a failed candidate and
must not be described as completed or silently dropped.

The confirmation dimensions are pinned, not re-selected after the retrieval
scope correction: PDF fixed-800/fixed-1200, dense/hybrid-rrf,
pdf-only/hierarchical-pdf, and rerank-off/rerank-50-to-10. This keeps the
global diagnostic and paper-scoped correction on the same 35-candidate matrix.
The chained stage anchors are likewise pinned to fixed-1200,
note-reviewer-concern, hybrid-rrf, and pdf-only. The sparse reviewer-concern
note candidate remains diagnostic-only; retaining it as the correction-run
anchor preserves exact config-ID comparability and does not make it eligible.

The manifest must expose this reduction as `confirmation_plan`: 16 Cartesian
rows, 12 unique candidates, 4 deduplicated compatibility aliases, and the
stable rule `hierarchical-pdf-requires-pdf-parent-child`. This makes the
35-candidate total auditable without pretending that identical executions are
independent experiments.

The effective publication state must contain no pending, running, failed, or
blocked tasks. A historical terminal outer failure must never be rewritten.
It may be superseded only by `reconciliation.json`, whose internal envelope
hash-binds the original `run-state.json`, the audited 35-candidate matrix,
the N0/N3/N1 pre-quality result, every approved extension, and all aggregate
report inputs. This is distinct from a terminal strategy failure inside the
35-candidate sweep. Evidence mapping must cover exactly the 20 rq-2 papers,
record both mapped and total evidence-group counts, and pass the configured
overall and per-paper thresholds.

The public data fingerprint must bind the audited source manifests, the
254-question suite, and all 20 frozen-note hashes. Hardware fingerprints must
be non-empty, stable, and limited to hashed platform, CPU, and GPU
descriptions; they must not expose hostnames, usernames, serial numbers, raw
WMI output, or local paths. The manifest must also list the basename, byte
count, and SHA-256 of the other six public report files. The manifest itself
is not self-hashed.

The public manifest must be rebuilt from allowlisted fields rather than
copying the internal runtime summary, which contains run-local absolute paths
and cache locations.

`morning-report.md` must distinguish total, completed, failed, incomplete, and
rankable candidates. It must not describe an explicit failed or ineligible
strategy record as completed. It must disclose that the winner remains
provisional and that sustained latency measurements may reflect the recorded
thermal steady state.

The rq-2 sweep is a provisional strategy-selection run. `guardrails_passed`
means that the registered metric bundle is finite and that the candidate
passed the pinned relative baseline policy: no more than one domain may
regress by over two percentage points, multi-hop and referenced-adversarial
slices may not regress by over two percentage points, overall Recall@10 and
all-required-groups success@10 may not regress by over 0.5 percentage points,
and no new Recall@10 hard failure is allowed. A completed candidate may fail
these guardrails and must remain reportable, but it is ineligible for ranking,
Pareto membership, or provisional-winner selection.

Latency or index growth over 1.5x is recorded as an operational-review warning
rather than silently treated as production-ready. The public report must state
that such a candidate still needs an explicit quality justification and
rollback switch. It must not claim that the provisional rq-2 winner is already
approved as a production default.

The PDF chunking arm evaluates each ResearchQA paper's `Main` benchmark PDF.
All acquired SI and auxiliary files are parsed and are mandatory inputs to
generic-note generation; note-based strategies therefore contain SI-derived
content. Direct SI/native-source chunk retrieval is not evaluated in rq-2, so
the public report must not claim direct SI retrieval performance.

`blocked-and-unmapped.jsonl` must never be copied verbatim from the ignored
run directory. Its public form must remove evidence `alternatives` and redact
path-like error details, retaining only stable IDs, stage/config status, and
gate outcomes.

Do not publish ResearchQA JSONL rows or derived question subsets, PDFs or SI,
extracted source text, chunks, embeddings, indexes, per-question results,
model caches, raw traces, or generated benchmark notes. Those remain under the
ignored local cache and retain their upstream licenses.

## Export contract

The rq-2 public exporter is deliberately run-specific. It must:

1. refuse any run without a valid superseding reconciliation or whose 35
   frozen candidates and four approved extensions are not all terminal;
2. verify candidate, progress, reconciliation, and aggregate-artifact hashes,
   plus the common evaluable-set/mapping gates, before reading report values;
3. parse and rewrite aggregate rows through explicit field allowlists instead
   of copying files or directories;
4. build all eight files in a temporary directory, validate the manifest and
   sibling hashes, scan for forbidden path/text fields, and only then replace
   `researchqa-rq2/` atomically; and
5. leave the existing public directory unchanged on any validation failure.

The exporter must not accept a generic source-directory copy mode. Its CLI
must require an explicit completed rq-2 run root and use the repository's
pinned benchmark configuration. No network or model call is permitted during
export.

After the run has reached every completion gate, export with:

```powershell
C:\Users\Link\AppData\Local\Programs\Python\Python311\python.exe `
  benchmarks\scripts\export_rq2_public_report.py `
  --run-root benchmarks\.cache\researchqa\runs\<run-id>
```

The command validates and rewrites aggregate rows into a staged directory,
checks all sibling hashes and the public manifest, runs the privacy scan, and
then replaces only `benchmarks/reports/researchqa-rq2/`.
