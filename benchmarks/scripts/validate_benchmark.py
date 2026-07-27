#!/usr/bin/env python3
"""Validate the versioned research-rag benchmark contract.

This command is intentionally read-only. It validates JSON Schema structure,
cross-file references, suite partition invariants, and (optionally) release
quotas. It never opens PDFs, Zotero, ChromaDB, user notes, or production state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Iterable

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError


BENCHMARK_ROOT = Path(__file__).resolve().parents[1]

JSONL_CONTRACTS = {
    "corpus/manifest.jsonl": ("manifest", "manifest.schema.json"),
    "gold/answers.jsonl": ("answers", "answer.schema.json"),
    "gold/claims.jsonl": ("claims", "claim.schema.json"),
    "gold/evidence_units.jsonl": ("evidence", "evidence-unit.schema.json"),
    "queries/queries.jsonl": ("queries", "query.schema.json"),
    "queries/document_qrels.jsonl": (
        "document_qrels",
        "document-qrel.schema.json",
    ),
    "queries/evidence_qrels.jsonl": (
        "evidence_qrels",
        "evidence-qrel.schema.json",
    ),
    "queries/judgment_pools.jsonl": (
        "judgment_pools",
        "judgment-pool.schema.json",
    ),
}

REQUIRED_SUITES = {"s5", "d20", "v20", "h60", "s100"}
PARTITION_SUITES = ("d20", "v20", "h60")
DOMAINS = {
    "catalysis-materials",
    "biomedicine",
    "cs-ml",
    "environment-energy-geoscience",
    "social-science-economics",
}


@dataclass
class ValidationResult:
    """Machine- and human-readable result returned by the validator."""

    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors


def _json_path(parts: Iterable[Any]) -> str:
    rendered = "$"
    for part in parts:
        if isinstance(part, int):
            rendered += f"[{part}]"
        else:
            rendered += f".{part}"
    return rendered


def _load_schema(path: Path, result: ValidationResult) -> dict[str, Any] | None:
    try:
        schema = json.loads(path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        return schema
    except FileNotFoundError:
        result.errors.append(f"{path}: missing schema")
    except json.JSONDecodeError as exc:
        result.errors.append(f"{path}:{exc.lineno}: invalid schema JSON: {exc.msg}")
    except SchemaError as exc:
        result.errors.append(f"{path}: invalid JSON Schema: {exc.message}")
    return None


def _read_jsonl(path: Path, result: ValidationResult) -> list[tuple[int, dict[str, Any]]]:
    records: list[tuple[int, dict[str, Any]]] = []
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        result.errors.append(f"{path}: missing contract data file")
        return records

    for line_number, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            result.errors.append(f"{path}:{line_number}: invalid JSON: {exc.msg}")
            continue
        if not isinstance(value, dict):
            result.errors.append(f"{path}:{line_number}: record must be a JSON object")
            continue
        records.append((line_number, value))
    return records


def _validate_jsonl(
    path: Path,
    schema: dict[str, Any],
    result: ValidationResult,
) -> list[tuple[int, dict[str, Any]]]:
    records = _read_jsonl(path, result)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for line_number, record in records:
        errors = sorted(
            validator.iter_errors(record),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        for error in errors:
            location = _json_path(error.absolute_path)
            result.errors.append(f"{path}:{line_number}:{location}: {error.message}")
    return records


def _read_suites(
    root: Path,
    schema: dict[str, Any] | None,
    result: ValidationResult,
) -> dict[str, dict[str, Any]]:
    suites: dict[str, dict[str, Any]] = {}
    if schema is None:
        return suites

    suite_dir = root / "suites"
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    for path in sorted(suite_dir.glob("*.yaml")):
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            result.errors.append(f"{path}: invalid suite YAML: {exc}")
            continue
        if not isinstance(value, dict):
            result.errors.append(f"{path}: suite must be a YAML mapping")
            continue
        for error in sorted(
            validator.iter_errors(value),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        ):
            result.errors.append(
                f"{path}:{_json_path(error.absolute_path)}: {error.message}"
            )
        suite_id = value.get("suite_id")
        if not isinstance(suite_id, str):
            continue
        if suite_id in suites:
            result.errors.append(f"{path}: duplicate suite_id {suite_id!r}")
        suites[suite_id] = value
        if path.stem != suite_id:
            result.errors.append(
                f"{path}: filename must match suite_id {suite_id!r}"
            )

    missing = REQUIRED_SUITES - set(suites)
    if missing:
        result.errors.append(f"{suite_dir}: missing suites: {sorted(missing)}")
    return suites


def _read_configs(
    root: Path,
    schema: dict[str, Any] | None,
    result: ValidationResult,
) -> dict[str, dict[str, Any]]:
    configs: dict[str, dict[str, Any]] = {}
    if schema is None:
        return configs

    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    config_dir = root / "configs"
    for path in sorted(config_dir.glob("*.yaml")):
        try:
            value = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            result.errors.append(f"{path}: invalid config YAML: {exc}")
            continue
        if not isinstance(value, dict):
            result.errors.append(f"{path}: config must be a YAML mapping")
            continue
        for error in sorted(
            validator.iter_errors(value),
            key=lambda item: tuple(str(part) for part in item.absolute_path),
        ):
            result.errors.append(
                f"{path}:{_json_path(error.absolute_path)}: {error.message}"
            )
        config_id = value.get("config_id")
        if not isinstance(config_id, str):
            continue
        if config_id in configs:
            result.errors.append(f"{path}: duplicate config_id {config_id!r}")
        configs[config_id] = value
    if not configs:
        result.errors.append(f"{config_dir}: at least one benchmark config is required")
    return configs


def _index_unique(
    records: list[tuple[int, dict[str, Any]]],
    key: str,
    label: str,
    result: ValidationResult,
) -> dict[str, dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    for line_number, record in records:
        value = record.get(key)
        if not isinstance(value, str):
            continue
        if value in indexed:
            result.errors.append(
                f"{label}:{line_number}: duplicate {key} {value!r}"
            )
        else:
            indexed[value] = record
    return indexed


def _is_safe_artifact_path(value: str) -> bool:
    windows = PureWindowsPath(value)
    posix = PurePosixPath(value.replace("\\", "/"))
    return (
        not windows.is_absolute()
        and not posix.is_absolute()
        and ".." not in windows.parts
        and ".." not in posix.parts
    )


def _check_manifest(
    papers: dict[str, dict[str, Any]],
    result: ValidationResult,
) -> dict[str, str]:
    file_to_paper: dict[str, str] = {}
    for paper_id, paper in papers.items():
        files = [paper.get("main_pdf")] + list(paper.get("si") or [])
        for file_record in files:
            if not isinstance(file_record, dict):
                continue
            file_id = file_record.get("file_id")
            if not isinstance(file_id, str):
                continue
            if file_id in file_to_paper:
                result.errors.append(
                    f"manifest: file_id {file_id!r} is used by multiple papers"
                )
            else:
                file_to_paper[file_id] = paper_id
            artifact_path = file_record.get("artifact_path")
            if isinstance(artifact_path, str) and not _is_safe_artifact_path(
                artifact_path
            ):
                result.errors.append(
                    f"manifest:{paper_id}:{file_id}: artifact_path must be a safe "
                    "relative path without '..'"
                )
    return file_to_paper


def _check_references(
    data: dict[str, list[tuple[int, dict[str, Any]]]],
    suites: dict[str, dict[str, Any]],
    result: ValidationResult,
) -> None:
    papers = _index_unique(data["manifest"], "paper_id", "manifest", result)
    queries = _index_unique(data["queries"], "query_id", "queries", result)
    claims = _index_unique(data["claims"], "claim_id", "claims", result)
    evidence = _index_unique(data["evidence"], "evidence_id", "evidence", result)
    answers = _index_unique(data["answers"], "query_id", "answers", result)
    file_to_paper = _check_manifest(papers, result)
    evidence_groups = {
        item.get("evidence_group_id")
        for item in evidence.values()
        if isinstance(item.get("evidence_group_id"), str)
    }

    for evidence_id, item in evidence.items():
        paper_id = item.get("paper_id")
        file_id = item.get("file_id")
        if paper_id not in papers:
            result.errors.append(
                f"evidence:{evidence_id}: unknown paper_id {paper_id!r}"
            )
        if file_to_paper.get(file_id) != paper_id:
            result.errors.append(
                f"evidence:{evidence_id}: file_id {file_id!r} does not belong "
                f"to paper_id {paper_id!r}"
            )
        quote = item.get("verbatim_quote")
        quote_hash = item.get("quote_hash")
        if isinstance(quote, str) and isinstance(quote_hash, str):
            actual = hashlib.sha256(quote.encode("utf-8")).hexdigest()
            if actual != quote_hash:
                result.errors.append(
                    f"evidence:{evidence_id}: quote_hash does not match verbatim_quote"
                )
        locator = item.get("locator")
        if isinstance(locator, dict) and {
            "char_start",
            "char_end",
        } <= locator.keys():
            if locator["char_end"] <= locator["char_start"]:
                result.errors.append(
                    f"evidence:{evidence_id}: locator.char_end must exceed char_start"
                )

    for claim_id, item in claims.items():
        for paper_id in item.get("paper_ids") or []:
            if paper_id not in papers:
                result.errors.append(
                    f"claim:{claim_id}: unknown paper_id {paper_id!r}"
                )
        for group_id in item.get("evidence_group_ids") or []:
            if group_id not in evidence_groups:
                result.errors.append(
                    f"claim:{claim_id}: unknown evidence_group_id {group_id!r}"
                )

    for query_id, item in queries.items():
        for claim_id in item.get("expected_claim_ids") or []:
            if claim_id not in claims:
                result.errors.append(
                    f"query:{query_id}: unknown expected_claim_id {claim_id!r}"
                )
        for group_id in item.get("required_evidence_group_ids") or []:
            if group_id not in evidence_groups:
                result.errors.append(
                    f"query:{query_id}: unknown required evidence group {group_id!r}"
                )
        answer = answers.get(query_id)
        if answer is None:
            result.errors.append(f"query:{query_id}: missing answer key")
        elif set(answer.get("expected_claim_ids") or []) != set(
            item.get("expected_claim_ids") or []
        ):
            result.errors.append(
                f"query:{query_id}: answer/query expected_claim_ids differ"
            )

    for answer_query_id in answers:
        if answer_query_id not in queries:
            result.errors.append(
                f"answer:{answer_query_id}: answer references unknown query"
            )

    seen_qrels: set[tuple[str, str, str]] = set()
    for label, target_key, target_index in (
        ("document_qrels", "paper_id", papers),
        ("evidence_qrels", "evidence_id", evidence),
    ):
        for line_number, item in data[label]:
            query_id = item.get("query_id")
            target_id = item.get(target_key)
            if query_id not in queries:
                result.errors.append(
                    f"{label}:{line_number}: unknown query_id {query_id!r}"
                )
            if target_id not in target_index:
                result.errors.append(
                    f"{label}:{line_number}: unknown {target_key} {target_id!r}"
                )
            pair = (label, str(query_id), str(target_id))
            if pair in seen_qrels:
                result.errors.append(
                    f"{label}:{line_number}: duplicate judgment for {pair[1:]}"
                )
            seen_qrels.add(pair)

    for line_number, item in data["judgment_pools"]:
        query_id = item.get("query_id")
        target_kind = item.get("target_kind")
        target_id = item.get("target_id")
        if query_id not in queries:
            result.errors.append(
                f"judgment_pools:{line_number}: unknown query_id {query_id!r}"
            )
        targets = papers if target_kind == "document" else evidence
        if target_id not in targets:
            result.errors.append(
                f"judgment_pools:{line_number}: unknown {target_kind} "
                f"target_id {target_id!r}"
            )

    _check_suites(suites, papers, queries, result)


def _check_suites(
    suites: dict[str, dict[str, Any]],
    papers: dict[str, dict[str, Any]],
    queries: dict[str, dict[str, Any]],
    result: ValidationResult,
) -> None:
    if REQUIRED_SUITES - set(suites):
        return

    versions = {suite.get("benchmark_version") for suite in suites.values()}
    if len(versions) != 1:
        result.errors.append("suites: all benchmark_version values must match")

    for suite_id, suite in suites.items():
        for paper_id in suite.get("paper_ids") or []:
            if paper_id not in papers:
                result.errors.append(
                    f"suite:{suite_id}: unknown paper_id {paper_id!r}"
                )
        for query_id in suite.get("query_ids") or []:
            query = queries.get(query_id)
            if query is None:
                result.errors.append(
                    f"suite:{suite_id}: unknown query_id {query_id!r}"
                )
                continue
            if suite_id in PARTITION_SUITES and query.get("partition") != suite_id:
                result.errors.append(
                    f"suite:{suite_id}: query {query_id!r} has partition "
                    f"{query.get('partition')!r}"
                )

    s5_papers = set(suites["s5"].get("paper_ids") or [])
    d20_papers = set(suites["d20"].get("paper_ids") or [])
    s5_queries = set(suites["s5"].get("query_ids") or [])
    d20_queries = set(suites["d20"].get("query_ids") or [])
    if not s5_papers <= d20_papers:
        result.errors.append("suites: S5 paper_ids must be a subset of D20")
    if not s5_queries <= d20_queries:
        result.errors.append("suites: S5 query_ids must be a subset of D20")

    paper_partitions = {
        suite_id: set(suites[suite_id].get("paper_ids") or [])
        for suite_id in PARTITION_SUITES
    }
    query_partitions = {
        suite_id: set(suites[suite_id].get("query_ids") or [])
        for suite_id in PARTITION_SUITES
    }
    for index, left in enumerate(PARTITION_SUITES):
        for right in PARTITION_SUITES[index + 1 :]:
            if paper_partitions[left] & paper_partitions[right]:
                result.errors.append(
                    f"suites: {left}/{right} paper partitions must be disjoint"
                )
            if query_partitions[left] & query_partitions[right]:
                result.errors.append(
                    f"suites: {left}/{right} query partitions must be disjoint"
                )

    expected_s100_papers = set().union(*paper_partitions.values())
    expected_s100_queries = set().union(*query_partitions.values())
    if set(suites["s100"].get("paper_ids") or []) != expected_s100_papers:
        result.errors.append(
            "suites: S100 paper_ids must equal D20 + V20 + H60"
        )
    if set(suites["s100"].get("query_ids") or []) != expected_s100_queries:
        result.errors.append(
            "suites: S100 query_ids must equal D20 + V20 + H60"
        )


def _check_release_ready(
    data: dict[str, list[tuple[int, dict[str, Any]]]],
    suites: dict[str, dict[str, Any]],
    configs: dict[str, dict[str, Any]],
    result: ValidationResult,
) -> None:
    if REQUIRED_SUITES - set(suites):
        return
    expected_papers = {"s5": 5, "d20": 20, "v20": 20, "h60": 60, "s100": 100}
    minimum_queries = {"s5": 25, "d20": 100, "v20": 60, "h60": 140, "s100": 300}
    for suite_id, expected in expected_papers.items():
        actual = len(suites[suite_id].get("paper_ids") or [])
        if actual != expected:
            result.errors.append(
                f"release:{suite_id}: expected {expected} papers, found {actual}"
            )
    for suite_id, minimum in minimum_queries.items():
        actual = len(suites[suite_id].get("query_ids") or [])
        if actual < minimum:
            result.errors.append(
                f"release:{suite_id}: expected at least {minimum} queries, found {actual}"
            )

    papers = {
        record["paper_id"]: record
        for _, record in data["manifest"]
        if isinstance(record.get("paper_id"), str)
    }
    domain_targets = {"s5": 1, "d20": 4, "v20": 4, "h60": 12, "s100": 20}
    for suite_id, target in domain_targets.items():
        counts = {domain: 0 for domain in DOMAINS}
        for paper_id in suites[suite_id].get("paper_ids") or []:
            domain = papers.get(paper_id, {}).get("domain")
            if domain in counts:
                counts[domain] += 1
        for domain, actual in counts.items():
            if actual != target:
                result.errors.append(
                    f"release:{suite_id}:{domain}: expected {target} papers, "
                    f"found {actual}"
                )

    queries = {
        record["query_id"]: record
        for _, record in data["queries"]
        if isinstance(record.get("query_id"), str)
    }
    h60_queries = [
        queries[query_id]
        for query_id in suites["h60"].get("query_ids") or []
        if query_id in queries
    ]
    negative_count = sum(
        query.get("answerability") != "answerable" for query in h60_queries
    )
    if negative_count < 30:
        result.errors.append(
            f"release:h60: expected at least 30 negative queries, found {negative_count}"
        )
    for slice_id in ("exact-token", "si", "cross-language", "multi-hop"):
        actual = sum(slice_id in (query.get("slice_ids") or []) for query in h60_queries)
        if actual < 20:
            result.errors.append(
                f"release:h60:{slice_id}: expected at least 20 queries, found {actual}"
            )
    positive_claims = sum(
        len(query.get("expected_claim_ids") or [])
        for query in h60_queries
        if query.get("answerability") == "answerable"
    )
    if positive_claims < 100:
        result.errors.append(
            "release:h60: expected at least 100 positive expected claims, "
            f"found {positive_claims}"
        )
    for config_id, config in configs.items():
        serialized = json.dumps(config, sort_keys=True)
        if "pending-" in serialized:
            result.errors.append(
                f"release:config:{config_id}: pending fingerprints are not allowed"
            )


def validate_benchmark(
    root: str | Path = BENCHMARK_ROOT,
    *,
    allow_empty: bool = False,
    release_ready: bool = False,
) -> ValidationResult:
    """Validate one benchmark root without mutating it."""

    root = Path(root).resolve()
    result = ValidationResult()
    schema_root = root / "schemas"
    schemas: dict[str, dict[str, Any]] = {}
    schema_names = {schema_name for _, schema_name in JSONL_CONTRACTS.values()}
    schema_names.update(
        {"config.schema.json", "suite.schema.json", "report.schema.json"}
    )
    for schema_name in sorted(schema_names):
        schema = _load_schema(schema_root / schema_name, result)
        if schema is not None:
            schemas[schema_name] = schema

    data: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for relative_path, (label, schema_name) in JSONL_CONTRACTS.items():
        schema = schemas.get(schema_name)
        if schema is None:
            data[label] = []
            continue
        data[label] = _validate_jsonl(root / relative_path, schema, result)
        result.counts[label] = len(data[label])

    suites = _read_suites(root, schemas.get("suite.schema.json"), result)
    result.counts["suites"] = len(suites)
    configs = _read_configs(root, schemas.get("config.schema.json"), result)
    result.counts["configs"] = len(configs)
    _check_references(data, suites, result)

    core_labels = (
        "manifest",
        "answers",
        "claims",
        "evidence",
        "queries",
        "document_qrels",
        "evidence_qrels",
    )
    if not allow_empty:
        for label in core_labels:
            if not data[label]:
                result.errors.append(
                    f"{label}: no records; use --allow-empty only for Wave 0A"
                )

    if release_ready:
        _check_release_ready(data, suites, configs, result)
    return result


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Validate research-rag benchmark contracts and references."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=BENCHMARK_ROOT,
        help="Benchmark root (default: repository benchmarks directory).",
    )
    parser.add_argument(
        "--allow-empty",
        action="store_true",
        help="Allow the committed Wave 0A skeleton to contain no corpus records.",
    )
    parser.add_argument(
        "--release-ready",
        action="store_true",
        help="Also enforce S5/D20/V20/H60/S100 release quotas.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    result = validate_benchmark(
        args.root,
        allow_empty=args.allow_empty,
        release_ready=args.release_ready,
    )
    for warning in result.warnings:
        print(f"[WARN] {warning}")
    for error in result.errors:
        print(f"[ERROR] {error}")
    summary = ", ".join(f"{key}={value}" for key, value in sorted(result.counts.items()))
    if result.ok:
        print(f"[OK] benchmark contract valid ({summary})")
        return 0
    print(f"[FAIL] benchmark contract invalid: {len(result.errors)} error(s) ({summary})")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
