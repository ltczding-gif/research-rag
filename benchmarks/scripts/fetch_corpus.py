#!/usr/bin/env python3
"""Fetch or verify checksum-pinned public benchmark PDFs.

This command is a maintainer acquisition tool. Ordinary pull-request CI must
consume a separately pinned offline corpus artifact and must not download from
publisher sites.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.request
from collections.abc import Iterable
from pathlib import Path
from typing import Any


BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = BENCHMARK_ROOT / "corpus" / "manifest.jsonl"
USER_AGENT = "research-rag-benchmark-corpus/0.1 (+https://github.com/ltczding-gif/research-rag)"


def load_manifest(path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        if not raw_line.strip():
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
        if not isinstance(record, dict):
            raise ValueError(f"{path}:{line_number}: expected a JSON object")
        records.append(record)
    return records


def iter_files(record: dict[str, Any]) -> Iterable[dict[str, Any]]:
    yield record["main_pdf"]
    yield from record.get("si") or []


def resolve_artifact_path(corpus_root: Path, artifact_path: str) -> Path:
    root = corpus_root.resolve()
    target = (root / artifact_path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ValueError(
            f"artifact_path escapes corpus root: {artifact_path!r}"
        ) from exc
    return target


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_pdf(path: Path, expected_sha256: str) -> tuple[bool, str]:
    if not path.is_file():
        return False, "missing"
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            return False, "not a PDF"
    actual = sha256_file(path)
    if actual != expected_sha256:
        return False, f"sha256 mismatch: expected {expected_sha256}, found {actual}"
    return True, actual


def download_pdf(url: str, target: Path, *, timeout: float) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            with partial.open("wb") as output:
                while block := response.read(1024 * 1024):
                    output.write(block)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)


def run(
    *,
    manifest_path: Path,
    corpus_root: Path,
    paper_ids: set[str] | None,
    check_only: bool,
    force: bool,
    timeout: float,
) -> int:
    records = load_manifest(manifest_path)
    known_ids = {record.get("paper_id") for record in records}
    if paper_ids:
        missing_ids = sorted(paper_ids - known_ids)
        if missing_ids:
            print(f"[ERROR] unknown paper_id(s): {', '.join(missing_ids)}")
            return 2
        records = [record for record in records if record.get("paper_id") in paper_ids]

    failures = 0
    checked = 0
    for record in records:
        paper_id = record["paper_id"]
        for file_record in iter_files(record):
            checked += 1
            file_id = file_record["file_id"]
            target = resolve_artifact_path(corpus_root, file_record["artifact_path"])
            expected_sha256 = file_record["sha256"]
            valid, detail = verify_pdf(target, expected_sha256)
            if valid and not force:
                print(f"[OK] {paper_id}/{file_id} {detail}")
                continue
            if check_only:
                failures += 1
                print(f"[ERROR] {paper_id}/{file_id}: {detail}")
                continue

            print(f"[FETCH] {paper_id}/{file_id}")
            try:
                download_pdf(file_record["source_url"], target, timeout=timeout)
                valid, detail = verify_pdf(target, expected_sha256)
            except Exception as exc:  # noqa: BLE001 - CLI reports per-file failures
                valid, detail = False, str(exc)
            if valid:
                print(f"[OK] {paper_id}/{file_id} {detail}")
            else:
                failures += 1
                print(f"[ERROR] {paper_id}/{file_id}: {detail}")

    print(f"checked={checked} failures={failures}")
    return 1 if failures else 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--corpus-root",
        type=Path,
        default=BENCHMARK_ROOT / "corpus",
        help="Root used to resolve manifest artifact_path values.",
    )
    parser.add_argument(
        "--paper-id",
        action="append",
        dest="paper_ids",
        help="Fetch/check only this paper_id; repeat for multiple papers.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Verify local files without network access.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Download again even when the local checksum already matches.",
    )
    parser.add_argument("--timeout", type=float, default=120.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(
            manifest_path=args.manifest.resolve(),
            corpus_root=args.corpus_root.resolve(),
            paper_ids=set(args.paper_ids) if args.paper_ids else None,
            check_only=args.check_only,
            force=args.force,
            timeout=args.timeout,
        )
    except (OSError, ValueError, KeyError) as exc:
        print(f"[ERROR] {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
