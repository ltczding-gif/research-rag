#!/usr/bin/env python3
"""Prepare deterministic, nested ResearchQA benchmark tiers.

The pinned upstream JSONL and every derived suite remain under the ignored
benchmark cache. The repository commits only the source/selection contract and
this reproducible preparation code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Iterable

import yaml
from jsonschema import Draft202012Validator, FormatChecker


BENCHMARK_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = BENCHMARK_ROOT / "sources" / "researchqa.yaml"
DEFAULT_SCHEMA = BENCHMARK_ROOT / "schemas" / "researchqa-source.schema.json"
DEFAULT_OUTPUT_ROOT = BENCHMARK_ROOT / ".cache" / "researchqa"

REQUIRED_ROW_FIELDS = {
    "paper_id",
    "paper_doi",
    "paper_s3_url",
    "domain",
    "row_id",
    "question_type",
    "question",
    "expected_answer",
    "expected_references",
}


class ResearchQAContractError(ValueError):
    """Raised when the pinned source or a derived suite violates the contract."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256_path(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
            total += len(block)
    return total, digest.hexdigest()


def load_contract(
    config_path: Path = DEFAULT_CONFIG,
    schema_path: Path = DEFAULT_SCHEMA,
) -> dict[str, Any]:
    """Load and schema-validate the committed ResearchQA contract."""

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(config),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        rendered = "; ".join(error.message for error in errors)
        raise ResearchQAContractError(f"invalid ResearchQA config: {rendered}")

    tier_ids = [tier["tier_id"] for tier in config["selection"]["tiers"]]
    if tier_ids != ["rq-2", "rq-5", "rq-10", "rq-all"]:
        raise ResearchQAContractError(
            "ResearchQA tiers must be ordered rq-2, rq-5, rq-10, rq-all"
        )
    if len(tier_ids) != len(set(tier_ids)):
        raise ResearchQAContractError("ResearchQA tier IDs must be unique")
    return config


def verify_source(path: Path, contract: dict[str, Any]) -> None:
    """Fail closed unless the source bytes match the pinned upstream snapshot."""

    expected = contract["source"]
    size, digest = _sha256_path(path)
    if size != expected["bytes"]:
        raise ResearchQAContractError(
            f"ResearchQA byte-size mismatch: expected {expected['bytes']}, found {size}"
        )
    if digest != expected["sha256"]:
        raise ResearchQAContractError(
            "ResearchQA SHA-256 mismatch: "
            f"expected {expected['sha256']}, found {digest}"
        )


def ensure_source(
    contract: dict[str, Any],
    output_root: Path,
    *,
    source_path: Path | None = None,
    offline: bool = False,
) -> Path:
    """Return a verified source path, downloading only the pinned file if needed."""

    if source_path is not None:
        resolved = source_path.resolve()
        verify_source(resolved, contract)
        return resolved

    destination = output_root / "source" / contract["source"]["filename"]
    if destination.is_file():
        verify_source(destination, contract)
        return destination
    if offline:
        raise ResearchQAContractError(
            f"offline mode requires the pinned source at {destination}"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    partial.unlink(missing_ok=True)
    try:
        with urllib.request.urlopen(
            contract["source"]["download_url"],
            timeout=120,
        ) as response, partial.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
        verify_source(partial, contract)
        partial.replace(destination)
    finally:
        partial.unlink(missing_ok=True)
    return destination


def _load_rows(
    source_path: Path,
    contract: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    rows: list[dict[str, Any]] = []
    row_ids: set[str] = set()
    papers: dict[str, dict[str, Any]] = {}
    domain_question_counts: Counter[str] = Counter()
    question_type_counts: Counter[str] = Counter()
    domain_papers: dict[str, set[str]] = defaultdict(set)

    with source_path.open("r", encoding="utf-8") as handle:
        for line_number, raw in enumerate(handle, 1):
            if not raw.strip():
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ResearchQAContractError(
                    f"{source_path}:{line_number}: invalid JSON: {exc.msg}"
                ) from exc
            if not isinstance(row, dict):
                raise ResearchQAContractError(
                    f"{source_path}:{line_number}: row must be an object"
                )
            missing = sorted(REQUIRED_ROW_FIELDS - set(row))
            if missing:
                raise ResearchQAContractError(
                    f"{source_path}:{line_number}: missing fields {missing}"
                )

            row_id = row["row_id"]
            if row_id in row_ids:
                raise ResearchQAContractError(f"duplicate row_id {row_id!r}")
            row_ids.add(row_id)

            paper_id = row["paper_id"]
            domain = row["domain"]
            metadata = {
                "paper_id": paper_id,
                "paper_doi": row["paper_doi"],
                "paper_s3_url": row["paper_s3_url"],
                "domain": domain,
            }
            previous = papers.setdefault(paper_id, metadata)
            if previous != metadata:
                raise ResearchQAContractError(
                    f"inconsistent metadata for paper_id {paper_id!r}"
                )

            rows.append(row)
            domain_question_counts[domain] += 1
            question_type_counts[row["question_type"]] += 1
            domain_papers[domain].add(paper_id)

    expected = contract["expected"]
    if len(rows) != expected["questions"]:
        raise ResearchQAContractError(
            f"expected {expected['questions']} questions, found {len(rows)}"
        )
    if len(papers) != expected["papers"]:
        raise ResearchQAContractError(
            f"expected {expected['papers']} papers, found {len(papers)}"
        )

    actual_domains = {
        domain: {
            "papers": len(domain_papers[domain]),
            "questions": domain_question_counts[domain],
        }
        for domain in sorted(domain_papers)
    }
    if actual_domains != expected["domains"]:
        raise ResearchQAContractError(
            "ResearchQA domain distribution differs from the pinned contract"
        )
    if dict(sorted(question_type_counts.items())) != expected["question_types"]:
        raise ResearchQAContractError(
            "ResearchQA question-type distribution differs from the pinned contract"
        )
    return rows, papers


def _selection_key(seed: str, domain: str, paper_id: str) -> tuple[str, str]:
    payload = f"{seed}\0{domain}\0{paper_id}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest(), paper_id


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(_canonical_json(record))
            handle.write("\n")


def build_tiers(
    contract: dict[str, Any],
    source_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Validate the source and write nested paper/question indexes for every tier."""

    verify_source(source_path, contract)
    rows, papers = _load_rows(source_path, contract)
    seed = contract["selection"]["seed"]

    papers_by_domain: dict[str, list[str]] = defaultdict(list)
    for paper_id, metadata in papers.items():
        papers_by_domain[metadata["domain"]].append(paper_id)
    for domain, paper_ids in papers_by_domain.items():
        paper_ids.sort(key=lambda paper_id: _selection_key(seed, domain, paper_id))

    rows_by_paper: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        rows_by_paper[row["paper_id"]].append(row)
    for paper_rows in rows_by_paper.values():
        paper_rows.sort(key=lambda row: row["row_id"])

    index: dict[str, Any] = {
        "schema_version": 1,
        "benchmark_id": contract["benchmark_id"],
        "source_revision": contract["source"]["revision"],
        "source_sha256": contract["source"]["sha256"],
        "selection_seed": seed,
        "tiers": {},
    }
    previous_ids: set[str] = set()

    for tier in contract["selection"]["tiers"]:
        limit = tier["papers_per_domain"]
        selected_by_domain = {
            domain: paper_ids if limit == "all" else paper_ids[:limit]
            for domain, paper_ids in sorted(papers_by_domain.items())
        }
        selected_ids = {
            paper_id
            for paper_ids in selected_by_domain.values()
            for paper_id in paper_ids
        }
        if previous_ids and not previous_ids <= selected_ids:
            raise ResearchQAContractError(
                f"{tier['tier_id']} is not a superset of the previous tier"
            )
        previous_ids = selected_ids
        if len(selected_ids) != tier["expected_papers"]:
            raise ResearchQAContractError(
                f"{tier['tier_id']}: expected {tier['expected_papers']} papers, "
                f"found {len(selected_ids)}"
            )

        selected_rows = [
            row
            for paper_id in sorted(selected_ids)
            for row in rows_by_paper[paper_id]
        ]
        if len(selected_rows) != tier["expected_questions"]:
            raise ResearchQAContractError(
                f"{tier['tier_id']}: expected {tier['expected_questions']} questions, "
                f"found {len(selected_rows)}"
            )
        question_types = Counter(row["question_type"] for row in selected_rows)
        paper_records = []
        for domain, paper_ids in selected_by_domain.items():
            for rank, paper_id in enumerate(paper_ids, 1):
                paper_rows = rows_by_paper[paper_id]
                paper_records.append(
                    {
                        **papers[paper_id],
                        "domain_rank": rank,
                        "question_count": len(paper_rows),
                        "question_types": dict(
                            sorted(
                                Counter(
                                    row["question_type"] for row in paper_rows
                                ).items()
                            )
                        ),
                    }
                )

        tier_root = output_root / "suites" / tier["tier_id"]
        _write_jsonl(tier_root / "papers.jsonl", paper_records)
        _write_jsonl(tier_root / "questions.jsonl", selected_rows)
        summary = {
            "schema_version": 1,
            "tier_id": tier["tier_id"],
            "purpose": tier["purpose"],
            "papers_per_domain": limit,
            "paper_count": len(selected_ids),
            "question_count": len(selected_rows),
            "domain_paper_counts": {
                domain: len(paper_ids)
                for domain, paper_ids in selected_by_domain.items()
            },
            "question_type_counts": dict(sorted(question_types.items())),
            "papers_file": f"suites/{tier['tier_id']}/papers.jsonl",
            "questions_file": f"suites/{tier['tier_id']}/questions.jsonl",
        }
        (tier_root / "suite.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        index["tiers"][tier["tier_id"]] = summary

    output_root.mkdir(parents=True, exist_ok=True)
    (output_root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return index


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prepare pinned, nested ResearchQA benchmark tiers."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Refuse network access and require a cached or explicit source file.",
    )
    parser.add_argument(
        "--check-only",
        action="store_true",
        help="Verify the pinned source and its distribution without writing suites.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        contract = load_contract(args.config)
        source_path = ensure_source(
            contract,
            args.output_root,
            source_path=args.source,
            offline=args.offline,
        )
        if args.check_only:
            _load_rows(source_path, contract)
            print(
                "[OK] ResearchQA source verified "
                f"(papers={contract['expected']['papers']}, "
                f"questions={contract['expected']['questions']})"
            )
            return 0
        index = build_tiers(contract, source_path, args.output_root)
    except (OSError, ResearchQAContractError, yaml.YAMLError) as exc:
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    rendered = ", ".join(
        f"{tier_id}={summary['paper_count']}p/{summary['question_count']}q"
        for tier_id, summary in index["tiers"].items()
    )
    print(f"[OK] ResearchQA tiers prepared ({rendered})")
    print(f"[OK] index: {args.output_root / 'index.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
