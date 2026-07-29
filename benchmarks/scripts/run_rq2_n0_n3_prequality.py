#!/usr/bin/env python3
"""Audit the frozen rq-2 N0/N3/N1 note route without loading models."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from benchmarks.researchqa_runtime import (  # noqa: E402
    ResearchQARuntimeError,
    run_n0_n3_prequality_runtime,
)
from benchmarks.scripts.run_researchqa_overnight import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_SCHEMA,
    load_config,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the offline N0 eligibility and N3 reviewer-parser audit "
            "for the frozen rq-2 corpus."
        )
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
    )
    parser.add_argument(
        "--schema",
        type=Path,
        default=DEFAULT_SCHEMA,
    )
    parser.add_argument("--run-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(
            args.config.resolve(strict=True),
            args.schema.resolve(strict=True),
        )
        result = run_n0_n3_prequality_runtime(
            config,
            args.run_root,
        )
    except (OSError, ValueError, ResearchQARuntimeError) as exc:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error": {
                        "type": type(exc).__name__,
                        "message": str(exc),
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "status": "completed",
                "candidate_config_id": result.candidate_config_id,
                "prequality_path": result.prequality_path,
                "diagnostics": {
                    "paper_count": result.diagnostics["paper_count"],
                    "eligible_paper_ids": (
                        result.diagnostics["eligible_paper_ids"]
                    ),
                    "fallback_paper_ids": (
                        result.diagnostics["fallback_paper_ids"]
                    ),
                    "base_chunk_count": (
                        result.diagnostics["base_chunk_count"]
                    ),
                    "reviewer_chunk_count": (
                        result.diagnostics["reviewer_chunk_count"]
                    ),
                    "reviewer_verdict_row_count": (
                        result.diagnostics["reviewer_verdict_row_count"]
                    ),
                    "reviewer_severity_counts": (
                        result.diagnostics["reviewer_severity_counts"]
                    ),
                    "reviewer_multi_claim_row_count": (
                        result.diagnostics[
                            "reviewer_multi_claim_row_count"
                        ]
                    ),
                    "diagnostic_fingerprint": (
                        result.diagnostics["diagnostic_fingerprint"]
                    ),
                },
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
