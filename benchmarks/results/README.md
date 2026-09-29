# Retrieval score ledger

`retrieval-ledger.jsonl` records every run of
`benchmarks/scripts/retrieval_eval.py run`, one JSON object per line, in the
`retrieval-eval-run-v1` format described in
[docs/RETRIEVAL_EVAL.md](../../docs/RETRIEVAL_EVAL.md).

Records hold IDs, settings and numbers only (no query or passage text, paths
or parent keys), so runs on private eval sets can be committed here and
compared over time:

```bash
python benchmarks/scripts/retrieval_eval.py compare --suite-id <eval set id>
```

Append-only: do not edit or delete past runs. A mistaken run can be superseded
by a later run with a `--label` explaining why.
