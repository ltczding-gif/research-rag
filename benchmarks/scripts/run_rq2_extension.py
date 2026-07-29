#!/usr/bin/env python3
"""Run one approved rq-2 extension against the frozen paper-scoped baseline."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from benchmarks.researchqa_runtime import (  # noqa: E402
    ResearchQARuntimeError,
    run_researchqa_extension_runtime,
)
from benchmarks.scripts.run_researchqa_overnight import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_SCHEMA,
    CliContractError,
    load_config,
)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one approved rq-2 extension with atomic candidate resume."
        )
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument(
        "--extension",
        choices=("F2", "RR1"),
        required=True,
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = load_config(args.config, args.schema)
        result = run_researchqa_extension_runtime(
            config,
            args.run_root,
            extension_id=args.extension,
        )
    except (CliContractError, ResearchQARuntimeError, OSError) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    print(
        json.dumps(
            {
                "extension_id": result.extension_id,
                "status": result.record.status,
                "config_id": result.record.candidate.config_id,
                "primary_score": result.record.primary,
                "guardrails_passed": result.record.guardrails_passed,
                "resumed": result.record.resumed,
                "result_path": result.record.result_path,
                "model_preflight_path": result.model_preflight_path,
                "prequality_path": result.prequality_path,
                "runtime_summary_path": result.runtime_summary_path,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
