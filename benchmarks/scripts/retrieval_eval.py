#!/usr/bin/env python3
"""Build retrieval eval sets and record/compare retrieval scores.

Subcommands
  pool     attach pooled retrieval candidates to each query for judging
  resolve  turn quotes and judged candidates into canonical evidence spans
  run      score strategies on an eval set and append the runs to the ledger
  compare  print recorded runs for one eval set with paired deltas

All commands except `compare` read the active canonical papers generation
configured by .env / LOCALRAG_* (the same index the MCP server serves).
See docs/RETRIEVAL_EVAL.md.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks import retrieval_eval as ev  # noqa: E402


def load_core():
    """Import the query core bound to the configured, active generations."""
    service = str(REPO_ROOT / "service")
    if service not in sys.path:
        sys.path.insert(0, service)
    import query_server  # noqa: WPS433 - initializes Chroma from config

    if query_server.pdf_generation is None:
        errors = getattr(query_server, "_index_errors", {})
        raise SystemExit("No canonical papers generation is active: "
                         + json.dumps(errors, ensure_ascii=False))
    return query_server


def load_suite(args) -> ev.Suite:
    if args.official:
        return ev.load_official_suite(REPO_ROOT / "benchmarks", args.official)
    if not args.suite:
        raise SystemExit("Give --suite PATH or --official SUITE_ID")
    return ev.load_suite(args.suite, args.suite_id)


def write_suite(suite: ev.Suite, path: str) -> None:
    text = "".join(json.dumps(ev.dump_query(q), ensure_ascii=False) + "\n" for q in suite.queries)
    Path(path).write_text(text, encoding="utf-8")


def cmd_pool(args) -> int:
    core = load_core()
    suite = ev.pool_candidates(core, load_suite(args), args.strategy or ["dense"], args.depth)
    write_suite(suite, args.output)
    total = sum(len(q.candidates) for q in suite.queries)
    print(f"Wrote {total} candidates for {len(suite.queries)} queries to {args.output}.")
    print("Set each candidate's relevance (0-3) and, for relevant ones, the minimal supporting quote;")
    print("then run: retrieval_eval.py resolve --suite <that file> --output <eval set>.")
    return 0


def cmd_resolve(args) -> int:
    core = load_core()
    suite = load_suite(args)
    suite, problems = ev.resolve_suite(suite, ev.load_pages(core.pdf_generation),
                                       min_relevance=args.min_relevance,
                                       allow_chunk_spans=args.allow_chunk_spans)
    write_suite(suite, args.output)
    spans = sum(len(q.evidence) for q in suite.queries)
    print(f"Wrote {len(suite.queries)} queries with {spans} evidence spans to {args.output}.")
    for problem in problems:
        print("  unresolved:", problem, file=sys.stderr)
    return 1 if problems else 0


def cmd_run(args) -> int:
    core = load_core()
    suite = load_suite(args)
    ks = sorted(set(args.k or ev.DEFAULT_KS))
    if 10 not in ks:
        print("note: primary metric span_coverage@10 needs k=10; adding it.", file=sys.stderr)
        ks = sorted({*ks, 10})
    for strategy in args.strategy or ["dense"]:
        record = ev.run_strategy(core, suite, strategy, ks=ks, repetitions=args.repetitions,
                                 packet_budget=args.packet_budget or None,
                                 allow_unscorable=args.allow_unscorable,
                                 progress=(lambda m: print(m, file=sys.stderr)) if args.verbose else None)
        if args.label:
            record["label"] = args.label
        if args.details:
            Path(args.details).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        if not args.no_ledger:
            ev.append_ledger(record, args.ledger)
        overall = record["metrics"]["overall"]
        print(f"{record['run_id']}: " + ", ".join(
            f"{name}={overall[name]:.3f}" for name in
            (f"span_coverage@{k}" for k in ks) if overall.get(name) is not None)
            + (f", packet_span_coverage={overall['packet_span_coverage']:.3f}"
               if overall.get("packet_span_coverage") is not None else "")
            + f", mrr={overall['mrr'] or 0:.3f}")
        if record["unscorable"]:
            print(f"  warning: {len(record['unscorable'])} gold spans were excluded as unscorable; this run "
                  "pairs only with runs that scored the same evidence.", file=sys.stderr)
    if not args.no_ledger:
        print(f"Recorded in {args.ledger}")
    return 0


def cmd_compare(args) -> int:
    records = [r for r in ev.read_ledger(args.ledger) if r["suite"]["suite_id"] == args.suite_id]
    if args.last:
        records = records[-args.last:]
        if args.baseline and not any(r["run_id"] == args.baseline for r in records):
            records = [r for r in ev.read_ledger(args.ledger) if r["run_id"] == args.baseline] + records
    metrics = args.metric or ["span_coverage@5", "span_coverage@10", "span_coverage@20",
                              "query_complete@10", "mrr", "packet_span_coverage"]
    print(ev.compare_table(records, metrics, args.baseline, args.primary))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def suite_args(p):
        p.add_argument("--suite", help="eval set JSONL (one query per line)")
        p.add_argument("--suite-id", help="name recorded in the ledger (default: file stem)")
        p.add_argument("--official", metavar="SUITE_ID", help="use benchmarks/ official ledgers, e.g. s5")

    pool = sub.add_parser("pool", help="attach pooled candidates for judging")
    suite_args(pool)
    pool.add_argument("--strategy", action="append", choices=sorted(ev.STRATEGIES))
    pool.add_argument("--depth", type=int, default=20)
    pool.add_argument("--output", required=True)
    pool.set_defaults(func=cmd_pool)

    resolve = sub.add_parser("resolve", help="resolve quotes and judgments to canonical spans")
    suite_args(resolve)
    resolve.add_argument("--output", required=True)
    resolve.add_argument("--min-relevance", type=int, default=ev.SCORED_RELEVANCE)
    resolve.add_argument("--allow-chunk-spans", action="store_true",
                         help="accept judged candidates without a quote, using the whole chunk as gold")
    resolve.set_defaults(func=cmd_resolve)

    run = sub.add_parser("run", help="score strategies and record them")
    suite_args(run)
    run.add_argument("--strategy", action="append", choices=sorted(ev.STRATEGIES))
    run.add_argument("--k", type=int, action="append", help="cutoffs (default 5, 10, 20)")
    run.add_argument("--repetitions", type=int, default=3, help="repeat each query to measure stability")
    run.add_argument("--packet-budget", type=int, default=8000,
                     help="also score prepare_answer packets at this budget (0 to skip)")
    run.add_argument("--label", help="free-text note stored with the run")
    run.add_argument("--ledger", default=str(ev.DEFAULT_LEDGER))
    run.add_argument("--details", help="also write the full run record to this JSON file")
    run.add_argument("--allow-unscorable", action="store_true",
                     help="score despite gold spans the active generation cannot score (excluded; "
                          "such runs pair only with runs that scored the same evidence)")
    run.add_argument("--no-ledger", action="store_true")
    run.add_argument("--verbose", action="store_true")
    run.set_defaults(func=cmd_run)

    compare = sub.add_parser("compare", help="compare recorded runs on one eval set")
    compare.add_argument("--suite-id", required=True)
    compare.add_argument("--ledger", default=str(ev.DEFAULT_LEDGER))
    compare.add_argument("--baseline", help="run_id to compare against (default: first run)")
    compare.add_argument("--metric", action="append", help="columns to show")
    compare.add_argument("--primary", default=ev.PRIMARY_METRIC, help="metric for paired deltas")
    compare.add_argument("--last", type=int, help="only the most recent N runs (plus the baseline)")
    compare.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ev.SuiteError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
