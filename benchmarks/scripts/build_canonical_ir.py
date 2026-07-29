#!/usr/bin/env python3
"""Build the Wave 1A page/span IR with the legacy C0 chunk adapter."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.canonical_ir import build_manifest_c0  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Build deterministic C0 chunk JSONL with physical-page provenance."
        )
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "corpus" / "manifest.jsonl",
    )
    parser.add_argument(
        "--corpus-root",
        type=Path,
        default=REPO_ROOT / "benchmarks" / "corpus",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Ignored JSONL artifact path to create.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    result = build_manifest_c0(args.manifest, args.corpus_root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="\n") as handle:
        for chunk in result.chunks:
            handle.write(
                json.dumps(
                    chunk.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            )
    print(json.dumps(result.summary(), sort_keys=True))
    print(f"output={args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
