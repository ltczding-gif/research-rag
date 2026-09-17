#!/usr/bin/env python3
"""Run a frozen retrieved-vs-oracle answer-context diagnostic bundle."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))
from benchmarks.answer_diagnostics import AnswerDiagnosticError, run_answer_diagnostics  # noqa: E402
def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--bundle-sha256", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--execute", action="store_true", help="Call local Ollama; default is dry-run.")
    parser.add_argument("--base-url", help="Only used when bundle model.endpoint is /api/chat.")
    parser.add_argument("--timeout-seconds", type=float, default=120.0)
    return parser.parse_args(argv)
def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        output = run_answer_diagnostics(
            args.bundle, args.output, expected_bundle_sha256=args.bundle_sha256,
            execute=args.execute, base_url=args.base_url, timeout_seconds=args.timeout_seconds,
        )
    except (AnswerDiagnosticError, OSError) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    manifest = json.loads((output / "run-manifest.json").read_text(encoding="utf-8"))
    print(json.dumps({"output": str(output), "execute": args.execute, "status": manifest["status"]}, ensure_ascii=False, sort_keys=True))
    return 0 if manifest["status"] in {"dry-run", "completed"} else 2
if __name__ == "__main__":
    raise SystemExit(main())
