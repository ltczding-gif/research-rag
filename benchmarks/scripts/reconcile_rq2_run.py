#!/usr/bin/env python3
"""Build the final rq-2 superseding reconciliation and aggregate reports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from benchmarks.researchqa_reconciliation import (  # noqa: E402
    ResearchQAReconciliationError,
    load_config,
    reconcile_rq2_run,
)
from benchmarks.scripts.run_researchqa_overnight import (  # noqa: E402
    DEFAULT_CONFIG,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Audit the frozen rq-2 matrix and approved extensions, then "
            "write a hash-bound superseding completion."
        )
    )
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    try:
        output = reconcile_rq2_run(
            args.run_root,
            load_config(args.config),
        )
    except (ResearchQAReconciliationError, OSError, ValueError) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "status": "completed",
                "reconciliation_path": str(output.resolve()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
