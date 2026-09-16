"""Write the read-only rq-2 candidate validity audit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.researchqa_validity_audit import (
    audit_strategy_run,
    write_audit_csv,
    write_audit_json,
)


DEFAULT_CONFIG = ROOT / "benchmarks" / "configs" / "rq2-overnight.yaml"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit all 35 persisted rq-2 strategy candidates."
    )
    parser.add_argument("--run-root", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--csv-out", type=Path)
    parser.add_argument("--json-out", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("benchmark config must be a mapping")
    audit = audit_strategy_run(args.run_root, config)
    if args.csv_out is not None:
        write_audit_csv(args.csv_out, audit.rows)
    if args.json_out is not None:
        write_audit_json(args.json_out, audit)
    print(
        json.dumps(
            {
                key: value
                for key, value in audit.to_dict().items()
                if key != "rows"
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if audit.baseline_validity_gate_closed else 2


if __name__ == "__main__":
    sys.exit(main())
