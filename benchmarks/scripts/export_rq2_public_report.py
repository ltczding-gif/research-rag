#!/usr/bin/env python3
"""Export one completed ResearchQA rq-2 run as public aggregate artifacts."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from benchmarks.researchqa_public_export import (  # noqa: E402
    DEFAULT_CONFIG,
    DEFAULT_OUTPUT,
    RQ2PublicExportError,
    export_rq2_public_report,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Validate and atomically export one completed rq-2 run. "
            "No corpus, question, note, model, or path data is published."
        )
    )
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        output = export_rq2_public_report(
            args.run_root,
            output_root=args.output,
            config_path=args.config,
        )
    except (OSError, ValueError, RQ2PublicExportError) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
