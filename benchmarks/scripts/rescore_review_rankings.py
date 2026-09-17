#!/usr/bin/env python3
"""Rescore preserved ResearchQA rankings into a new, explicitly versioned file."""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.overnight import canonical_json_bytes
from benchmarks.researchqa_review import load_verified_payload, rescore_payload
from benchmarks.researchqa_strategy import load_gold_adjudications, load_main_documents
from benchmarks.researchqa_sweep import _implementation_fingerprints


def implementation_snapshot():
    value = _implementation_fingerprints()
    value["rescore_sources"] = {
        relative: hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        for relative in ("benchmarks/researchqa_review.py", "benchmarks/scripts/rescore_review_rankings.py",
                         "benchmarks/configs/review-comparison-v1.json")
    }
    return value


def _load_adjudications_for_current_questions(args, questions, parser):
    if args.adjudications and (not args.dataset_id or not args.dataset_revision):
        parser.error("--adjudications requires --dataset-id and --dataset-revision")
    if not args.adjudications:
        return None
    return load_gold_adjudications(
        args.adjudications,
        questions=questions,
        dataset_id=args.dataset_id,
        dataset_revision=args.dataset_revision,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-run", type=Path, required=True)
    parser.add_argument("--questions", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, action="append", required=True)
    parser.add_argument("--historical-bindings", type=Path, required=True,
                        help="Independently verified historical artifact/input manifest; never generated from candidate bytes by this command")
    parser.add_argument("--adjudications", type=Path)
    parser.add_argument("--dataset-id")
    parser.add_argument("--dataset-revision")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("output already exists; use a new versioned output path")
    implementation = implementation_snapshot()
    bindings = json.loads(args.historical_bindings.read_text(encoding="utf-8"))
    questions_sha256 = hashlib.sha256(args.questions.read_bytes()).hexdigest()
    questions = [json.loads(line) for line in args.questions.read_text(encoding="utf-8").splitlines() if line.strip()]
    documents = load_main_documents(args.source_run)
    adjudications = _load_adjudications_for_current_questions(
        args, questions, parser
    )
    results = []
    for candidate_path in args.candidate:
        payload = load_verified_payload(candidate_path, bindings=bindings, questions_sha256=questions_sha256)
        result = rescore_payload(
            payload,
            documents,
            questions,
            adjudications=adjudications,
            dataset_id=args.dataset_id,
            dataset_revision=args.dataset_revision,
        )
        result["historical_artifact_sha256"] = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        results.append(result)
    common = results[0]["evaluable_set"]
    if any(result["evaluable_set"] != common for result in results[1:]):
        raise ValueError("Cannot compare different evaluable sets")
    report = {
        "schema_version": 1,
        "implementation": implementation,
        "questions_sha256": questions_sha256,
        "historical_bindings_sha256": hashlib.sha256(args.historical_bindings.read_bytes()).hexdigest(),
        "historical_bindings_provenance": bindings.get("provenance"),
        "adjudications_sha256": hashlib.sha256(args.adjudications.read_bytes()).hexdigest() if args.adjudications else None,
        "results": results,
    }
    if implementation_snapshot() != implementation:
        raise RuntimeError("Scoring implementation changed during run; rerun after edits finish")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as handle:
        handle.write(canonical_json_bytes(report))
    print(json.dumps({"output": str(args.output), "candidate_count": len(results),
                      "verified_groups": len(common)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
