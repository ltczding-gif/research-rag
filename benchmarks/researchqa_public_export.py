"""Fail-closed, run-specific exporter for the public ResearchQA rq-2 report."""

from __future__ import annotations

import csv
import json
import os
import platform
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from benchmarks.overnight import fingerprint_payload, sha256_path
from benchmarks.public_report import (
    PublicReportError,
    sanitize_rq2_blocked_rows,
    validate_rq2_public_manifest,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "benchmarks" / "configs" / "rq2-overnight.yaml"
DEFAULT_OUTPUT = REPO_ROOT / "benchmarks" / "reports" / "researchqa-rq2"
EXPECTED_STAGE_COUNTS = {
    "pdf-chunker": 7,
    "note-chunker": 4,
    "retriever": 3,
    "source-composition": 5,
    "reranker": 4,
    "top2-confirmation": 12,
}
LEADERBOARD_FIELDS = (
    "stage_id",
    "stage_rank",
    "config_id",
    "status",
    "rankable",
    "mapping_passed",
    "guardrails_passed",
    "primary_metric",
    "primary_score",
    "p95_latency_ms",
    "index_bytes",
    "chunk_count",
    "pdf_chunker",
    "note_chunker",
    "retriever",
    "source_composition",
    "reranker",
)
BREAKDOWN_BASE_FIELDS = ("role", "config_id", "scope", "key", "domain")
BREAKDOWN_METRIC_FIELDS = {
    "all_required_groups_success_at_10",
    "all_required_groups_success_at_5",
    "coverage_ndcg_at_10",
    "groups_covered_at_10",
    "groups_covered_at_5",
    "mrr",
    "recall_at_10",
    "recall_at_5",
}
PARETO_FIELDS = (
    "rank",
    "config_id",
    "stage_id",
    "primary",
    "p95_latency_ms",
    "index_bytes",
    "chunk_count",
    "status",
    "guardrails_passed",
)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FORBIDDEN_PUBLIC_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:[\\/]"),
    re.compile(r"(?i)(?:^|[^A-Za-z])/(?:Users|home)/"),
    re.compile(r"(?i)\\\\[A-Za-z0-9_.-]+\\"),
    re.compile(
        r'(?i)"(?:api[_-]?key|pdf_path|vault_path|document|query|question|'
        r'answer|alternatives|run_root|cache_root|hf_home)"\s*:'
    ),
)


class RQ2PublicExportError(RuntimeError):
    """Raised before the public directory is changed."""


def _read_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RQ2PublicExportError(f"{label} is unreadable: {path.name}") from exc
    if not isinstance(value, Mapping):
        raise RQ2PublicExportError(f"{label} must be a JSON object")
    return value


def _json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _write_bytes(path: Path, payload: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path


def _write_json(path: Path, value: object) -> Path:
    return _write_bytes(path, _json_bytes(value))


def _safe_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise RQ2PublicExportError(f"{label} is not a safe public identifier")
    return value


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RQ2PublicExportError(f"{label} is not numeric")
    number = float(value)
    if not (-float("inf") < number < float("inf")):
        raise RQ2PublicExportError(f"{label} is not finite")
    return number


def _load_config(path: Path) -> Mapping[str, Any]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise RQ2PublicExportError("pinned rq-2 config is unreadable") from exc
    if not isinstance(value, Mapping):
        raise RQ2PublicExportError("pinned rq-2 config must be an object")
    benchmark = value.get("benchmark")
    if (
        not isinstance(benchmark, Mapping)
        or benchmark.get("tier_id") != "rq-2"
        or benchmark.get("paper_count") != 20
        or benchmark.get("question_count") != 254
    ):
        raise RQ2PublicExportError("pinned rq-2 config has unexpected scope")
    return value


def _task_lists(state: Mapping[str, Any]) -> tuple[dict[str, int], dict[str, list[str]]]:
    tasks = state.get("tasks")
    if not isinstance(tasks, Mapping):
        raise RQ2PublicExportError("run state tasks are missing")
    statuses = ("pending", "running", "completed", "failed", "blocked")
    grouped = {status: [] for status in statuses}
    for task_id, raw in tasks.items():
        task = raw if isinstance(raw, Mapping) else {}
        status = task.get("status")
        if status not in grouped:
            raise RQ2PublicExportError("run state contains an unknown task status")
        grouped[str(status)].append(_safe_id(task_id, "task_id"))
    return (
        {status: len(grouped[status]) for status in statuses},
        {status: sorted(grouped[status]) for status in statuses},
    )


def _candidate_envelopes(
    run_root: Path,
) -> tuple[
    list[dict[str, object]],
    Mapping[str, object],
    tuple[str, ...],
    tuple[str, ...],
]:
    candidate_root = run_root / "sweep" / "candidates"
    paths = sorted(candidate_root.glob("*/*.json"))
    if len(paths) != 35:
        raise RQ2PublicExportError(
            f"expected 35 unique candidate files, found {len(paths)}"
        )
    stage_counts: Counter[str] = Counter()
    public_candidates: list[dict[str, object]] = []
    common_mapping: Mapping[str, object] | None = None
    expected_papers: tuple[str, ...] | None = None
    expected_questions: tuple[str, ...] | None = None
    seen: set[str] = set()
    for path in paths:
        envelope = _read_json(path, "candidate envelope")
        payload = envelope.get("payload")
        if not isinstance(payload, Mapping):
            raise RQ2PublicExportError("candidate payload is missing")
        if envelope.get("payload_sha256") != fingerprint_payload(payload):
            raise RQ2PublicExportError(
                f"candidate payload hash mismatch: {path.name}"
            )
        config_id = _safe_id(envelope.get("config_id"), "config_id")
        stage_id = _safe_id(envelope.get("stage_id"), "stage_id")
        status = _safe_id(envelope.get("status"), "candidate status")
        if (
            config_id in seen
            or stage_id not in EXPECTED_STAGE_COUNTS
            or status not in {"completed", "failed"}
            or path.stem != config_id
            or path.parent.name != stage_id
        ):
            raise RQ2PublicExportError("candidate identity/status is inconsistent")
        seen.add(config_id)
        stage_counts[stage_id] += 1

        if status == "completed":
            candidate = payload.get("candidate")
            mapping = payload.get("mapping")
            coverage = (
                mapping.get("coverage") if isinstance(mapping, Mapping) else None
            )
            papers = payload.get("completed_paper_ids")
            questions = payload.get("completed_question_ids")
            if (
                not isinstance(candidate, Mapping)
                or candidate.get("config_id") != config_id
                or candidate.get("stage_id") != stage_id
                or not isinstance(coverage, Mapping)
                or not isinstance(papers, list)
                or not isinstance(questions, list)
                or len(papers) != 20
                or len(questions) != 254
                or len(set(map(str, papers))) != 20
                or len(set(map(str, questions))) != 254
                or payload.get("guardrails_passed") is not True
            ):
                raise RQ2PublicExportError(
                    f"candidate completion gates failed: {config_id}"
                )
            current_papers = tuple(sorted(map(str, papers)))
            current_questions = tuple(sorted(map(str, questions)))
            if expected_papers is None:
                expected_papers = current_papers
                expected_questions = current_questions
                common_mapping = dict(coverage)
            elif (
                current_papers != expected_papers
                or current_questions != expected_questions
                or dict(coverage) != dict(common_mapping or {})
            ):
                raise RQ2PublicExportError(
                    f"candidate evaluable set/mapping differs: {config_id}"
                )
            public_candidates.append(
                {
                    "config_id": config_id,
                    "stage_id": stage_id,
                    "status": status,
                    "rankable": bool(candidate.get("rankable")),
                    "mapping_passed": bool(coverage.get("passed")),
                    "guardrails_passed": True,
                }
            )
        else:
            public_candidates.append(
                {
                    "config_id": config_id,
                    "stage_id": stage_id,
                    "status": status,
                    "rankable": stage_id != "note-chunker",
                    "mapping_passed": False,
                    "guardrails_passed": False,
                }
            )
    if dict(stage_counts) != EXPECTED_STAGE_COUNTS:
        raise RQ2PublicExportError("candidate stage counts are inconsistent")
    if common_mapping is None or expected_papers is None or expected_questions is None:
        raise RQ2PublicExportError("no completed candidate is available")
    public_candidates.sort(key=lambda row: (str(row["stage_id"]), str(row["config_id"])))
    return (
        public_candidates,
        common_mapping,
        expected_papers,
        expected_questions,
    )


def _rewrite_csv(
    source: Path,
    destination: Path,
    *,
    exact_fields: Sequence[str] | None = None,
    required_fields: Sequence[str] = (),
    allowed_fields: set[str] | None = None,
) -> tuple[str, ...]:
    try:
        with source.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            source_fields = tuple(reader.fieldnames or ())
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise RQ2PublicExportError(f"aggregate CSV is unreadable: {source.name}") from exc
    if exact_fields is not None:
        fields = tuple(exact_fields)
        if source_fields != fields:
            raise RQ2PublicExportError(f"{source.name} headers changed")
    else:
        fields = source_fields
        if (
            not set(required_fields).issubset(fields)
            or allowed_fields is None
            or not set(fields).issubset(allowed_fields)
        ):
            raise RQ2PublicExportError(f"{source.name} headers are unsafe")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    return fields


def _public_bootstrap(source: Path) -> dict[str, object]:
    raw = _read_json(source, "paired bootstrap")
    interval = raw.get("confidence_interval")
    if not isinstance(interval, list) or len(interval) != 2:
        raise RQ2PublicExportError("bootstrap confidence interval is missing")
    return {
        "schema_version": int(raw.get("schema_version", 1)),
        "metric": _safe_id(raw.get("metric"), "bootstrap metric"),
        "candidate_config_id": _safe_id(
            raw.get("candidate_config_id"), "bootstrap candidate"
        ),
        "baseline_config_id": _safe_id(
            raw.get("baseline_config_id"), "bootstrap baseline"
        ),
        "observed_delta": _finite(
            raw.get("observed_delta"), "bootstrap observed delta"
        ),
        "lower": _finite(interval[0], "bootstrap lower"),
        "upper": _finite(interval[1], "bootstrap upper"),
        "confidence": _finite(raw.get("confidence"), "bootstrap confidence"),
        "samples": int(raw.get("samples", 0)),
        "seed": _safe_id(raw.get("seed"), "bootstrap seed"),
    }


def _public_pareto(source: Path) -> dict[str, object]:
    raw = _read_json(source, "Pareto frontier")
    rows = raw.get("rows")
    if not isinstance(rows, list) or not rows:
        raise RQ2PublicExportError("Pareto frontier is empty")
    public_rows = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise RQ2PublicExportError("Pareto row is invalid")
        public = {field: row.get(field) for field in PARETO_FIELDS}
        _safe_id(public["config_id"], "Pareto config_id")
        _safe_id(public["stage_id"], "Pareto stage_id")
        _finite(public["primary"], "Pareto primary")
        _finite(public["p95_latency_ms"], "Pareto latency")
        public_rows.append(public)
    return {"schema_version": 1, "rows": public_rows}


def _blocked_rows(source: Path) -> list[dict[str, object]]:
    rows = []
    try:
        for line in source.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            value = json.loads(line)
            if not isinstance(value, Mapping):
                raise RQ2PublicExportError("blocked row is not an object")
            rows.append(value)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RQ2PublicExportError("blocked rows are unreadable") from exc
    try:
        return sanitize_rq2_blocked_rows(rows)
    except PublicReportError as exc:
        raise RQ2PublicExportError(str(exc)) from exc


def _data_fingerprint(run_root: Path) -> str:
    source_paths = sorted((run_root / "source").glob("W*/source-manifest.jsonl"))
    if len(source_paths) != 20:
        raise RQ2PublicExportError("data fingerprint requires 20 source manifests")
    source_hashes = []
    for path in source_paths:
        _size, digest = sha256_path(path)
        source_hashes.append((path.parent.name, digest))

    questions_path = run_root.parent.parent / "suites" / "rq-2" / "questions.jsonl"
    _question_size, question_digest = sha256_path(questions_path)

    frozen_root = run_root / "note-runs" / "frozen"
    frozen = frozen_root / "frozen-notes.jsonl"
    note_hashes = []
    try:
        rows = [
            json.loads(line)
            for line in frozen.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RQ2PublicExportError("frozen-note manifest is unreadable") from exc
    if len(rows) != 20:
        raise RQ2PublicExportError("data fingerprint requires 20 frozen notes")
    for row in rows:
        if not isinstance(row, Mapping):
            raise RQ2PublicExportError("frozen-note row is invalid")
        paper_id = _safe_id(row.get("paper_id"), "frozen paper_id")
        expected = row.get("note_sha256")
        if not isinstance(expected, str) or not _SHA256.fullmatch(expected):
            raise RQ2PublicExportError("frozen note SHA-256 is invalid")
        note_path = frozen_root / "notes" / f"{paper_id}.md"
        _size, actual = sha256_path(note_path)
        if actual != expected:
            raise RQ2PublicExportError(f"frozen note hash mismatch: {paper_id}")
        note_hashes.append((paper_id, expected))
    return fingerprint_payload(
        {
            "source_manifests": source_hashes,
            "questions_jsonl": question_digest,
            "frozen_notes": sorted(note_hashes),
        }
    )


def _hardware_fingerprints() -> dict[str, str]:
    platform_description = "|".join(
        (platform.system(), platform.release(), platform.machine())
    )
    cpu_description = (
        os.environ.get("PROCESSOR_IDENTIFIER")
        or platform.processor()
        or platform.machine()
    )
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version",
                "--format=csv,noheader,nounits",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
        gpu_description = "|".join(
            sorted(line.strip() for line in result.stdout.splitlines() if line.strip())
        )
    except (OSError, subprocess.SubprocessError):
        gpu_description = "nvidia-smi-unavailable"
    return {
        "platform": fingerprint_payload(platform_description),
        "cpu": fingerprint_payload(cpu_description),
        "gpu": fingerprint_payload(gpu_description),
    }


def _thermal_line(run_root: Path) -> str:
    path = run_root / "runtime" / "hardware-observations.json"
    if not path.is_file():
        return (
            "No sustained thermal observation record was supplied; interpret "
            "latency together with the hashed hardware identity."
        )
    value = _read_json(path, "hardware observation")
    maximum = _finite(value.get("max_gpu_temperature_c"), "maximum GPU temperature")
    target = _finite(value.get("target_temperature_c"), "target GPU temperature")
    software = value.get("software_thermal_slowdown_observed")
    hardware = value.get("hardware_thermal_slowdown_observed")
    if not isinstance(software, bool) or not isinstance(hardware, bool):
        raise RQ2PublicExportError("hardware thermal observations are invalid")
    return (
        f"Sustained latency measurements reached {maximum:.0f} C against a "
        f"{target:.0f} C target; software thermal slowdown observed: "
        f"{str(software).lower()}, hardware thermal slowdown observed: "
        f"{str(hardware).lower()}."
    )


def _morning_report(
    *,
    candidates: Sequence[Mapping[str, object]],
    bootstrap: Mapping[str, object],
    pareto: Mapping[str, object],
    run_root: Path,
) -> bytes:
    counts = Counter(str(row["status"]) for row in candidates)
    rankable = sum(bool(row["rankable"]) for row in candidates)
    winner = str(bootstrap["candidate_config_id"])
    pareto_rows = pareto.get("rows")
    winner_row = next(
        (
            row
            for row in pareto_rows
            if isinstance(row, Mapping) and row.get("config_id") == winner
        ),
        None,
    )
    if not isinstance(winner_row, Mapping):
        raise RQ2PublicExportError("bootstrap winner is absent from Pareto frontier")
    text = "\n".join(
        (
            "# ResearchQA rq-2 strategy report",
            "",
            f"- Unique candidates: {len(candidates)}",
            f"- Completed: {counts['completed']}",
            f"- Failed: {counts['failed']}",
            f"- Incomplete: {counts['incomplete']}",
            f"- Rankable: {rankable}",
            f"- Provisional winner: `{winner}`",
            f"- Winner coverage-nDCG@10: {float(winner_row['primary']):.6f}",
            f"- Winner p95 latency: {float(winner_row['p95_latency_ms']):.3f} ms",
            f"- Paired delta vs C0: {float(bootstrap['observed_delta']):+.6f} "
            f"(95% CI {float(bootstrap['lower']):+.6f} to "
            f"{float(bootstrap['upper']):+.6f}; "
            f"{int(bootstrap['samples']):,} domain-stratified paper resamples)",
            "",
            _thermal_line(run_root),
            "",
            "The PDF chunking arm retrieves only each paper's Main benchmark PDF. "
            "SI and auxiliary sources are mandatory inputs to generic-note "
            "generation, so note-based arms can contain SI-derived content; "
            "this run does not measure direct SI/native-source retrieval.",
            "",
            "The winner is provisional. The finite-metric guardrail used here is "
            "not the later production-migration regression gate, so this report "
            "does not approve a production default.",
            "",
            "This run stops after rq-2 and does not start rq-5 automatically.",
            "",
        )
    )
    return text.encode("utf-8")


def _artifact_rows(root: Path, names: Iterable[str]) -> list[dict[str, object]]:
    rows = []
    for name in sorted(names):
        size, digest = sha256_path(root / name)
        rows.append({"name": name, "bytes": size, "sha256": digest})
    return rows


def _privacy_scan(root: Path) -> None:
    for path in sorted(root.iterdir()):
        if not path.is_file():
            raise RQ2PublicExportError("public export contains a nested path")
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise RQ2PublicExportError(f"public file is not UTF-8: {path.name}") from exc
        for pattern in _FORBIDDEN_PUBLIC_PATTERNS:
            if pattern.search(text):
                raise RQ2PublicExportError(
                    f"privacy scan rejected public file: {path.name}"
                )


def _publish_directory(staged: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = target.parent / f".{target.name}.backup"
    if backup.exists():
        raise RQ2PublicExportError("stale public-report backup exists")
    moved_old = False
    try:
        if target.exists():
            os.replace(target, backup)
            moved_old = True
        os.replace(staged, target)
    except OSError as exc:
        if moved_old and backup.exists() and not target.exists():
            os.replace(backup, target)
        raise RQ2PublicExportError("public report replacement failed") from exc
    if backup.exists():
        shutil.rmtree(backup)


def export_rq2_public_report(
    run_root: str | Path,
    *,
    output_root: str | Path = DEFAULT_OUTPUT,
    config_path: str | Path = DEFAULT_CONFIG,
) -> Path:
    """Validate one completed run and atomically publish seven aggregate files."""

    run = Path(run_root).resolve(strict=True)
    output = Path(output_root).resolve(strict=False)
    config = _load_config(Path(config_path).resolve(strict=True))
    state = _read_json(run / "run-state.json", "run state")
    if state.get("status") != "completed":
        raise RQ2PublicExportError("outer run is not completed")
    fingerprints = state.get("fingerprints")
    if not isinstance(fingerprints, Mapping):
        raise RQ2PublicExportError("run fingerprints are missing")
    if fingerprints.get("config") != fingerprint_payload(config):
        raise RQ2PublicExportError("run/config fingerprint mismatch")
    task_counts, task_lists = _task_lists(state)
    if any(task_counts[key] for key in ("pending", "running", "failed", "blocked")):
        raise RQ2PublicExportError("outer task completion gate failed")

    candidates, mapping, _papers, _questions = _candidate_envelopes(run)
    report_root = run / "report"
    final_root = run / "sweep" / "final"
    decision = _read_json(final_root / "decision-summary.json", "decision summary")
    provisional_winner = _safe_id(
        decision.get("provisional_winner"), "provisional winner"
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    staged = Path(
        tempfile.mkdtemp(prefix=f".{output.name}.staged-", dir=output.parent)
    )
    moved = False
    try:
        _rewrite_csv(
            report_root / "leaderboard.csv",
            staged / "leaderboard.csv",
            exact_fields=LEADERBOARD_FIELDS,
        )
        _rewrite_csv(
            report_root / "paper-domain-breakdown.csv",
            staged / "paper-domain-breakdown.csv",
            required_fields=BREAKDOWN_BASE_FIELDS,
            allowed_fields=set(BREAKDOWN_BASE_FIELDS) | BREAKDOWN_METRIC_FIELDS,
        )
        bootstrap = _public_bootstrap(report_root / "paired-bootstrap.json")
        pareto = _public_pareto(final_root / "pareto-frontier.json")
        blocked = _blocked_rows(report_root / "blocked-and-unmapped.jsonl")
        _write_json(staged / "paired-bootstrap.json", bootstrap)
        _write_json(staged / "pareto-frontier.json", pareto)
        _write_bytes(
            staged / "blocked-and-unmapped.jsonl",
            b"".join(_json_bytes(row) for row in blocked),
        )
        _write_bytes(
            staged / "morning-report.md",
            _morning_report(
                candidates=candidates,
                bootstrap=bootstrap,
                pareto=pareto,
                run_root=run,
            ),
        )

        sibling_names = {
            "morning-report.md",
            "leaderboard.csv",
            "paper-domain-breakdown.csv",
            "paired-bootstrap.json",
            "pareto-frontier.json",
            "blocked-and-unmapped.jsonl",
        }
        public_manifest = {
            "schema_version": 1,
            "run_id": _safe_id(state.get("run_id"), "run_id"),
            "status": "completed",
            "created_at": str(state.get("created_at")),
            "updated_at": str(state.get("updated_at")),
            "budget_seconds": _finite(
                state.get("budget_seconds"), "budget_seconds"
            ),
            "elapsed_seconds": _finite(
                state.get("elapsed_seconds"), "elapsed_seconds"
            ),
            "fingerprints": {
                "code": _safe_id(fingerprints.get("code"), "code fingerprint"),
                "config": _safe_id(
                    fingerprints.get("config"), "config fingerprint"
                ),
                "embedding-model": _safe_id(
                    fingerprints.get("embedding-model"),
                    "embedding fingerprint",
                ),
                "reranker-model": _safe_id(
                    fingerprints.get("reranker-model"),
                    "reranker fingerprint",
                ),
                "data": _data_fingerprint(run),
            },
            "hardware_fingerprints": _hardware_fingerprints(),
            "task_counts": task_counts,
            "mapping_coverage": dict(mapping),
            "candidates": candidates,
            "confirmation_plan": {
                "cartesian_rows": 16,
                "unique_candidates": 12,
                "deduplicated_aliases": 4,
                "compatibility_rule": (
                    "hierarchical-pdf-requires-pdf-parent-child"
                ),
            },
            "bootstrap": bootstrap,
            "pareto_frontier": list(pareto["rows"]),
            "provisional_winner": provisional_winner,
            "artifacts": _artifact_rows(staged, sibling_names),
            "completed": task_lists["completed"],
            "partial": task_lists["pending"] + task_lists["running"],
            "blocked": task_lists["blocked"],
            "failed": task_lists["failed"],
        }
        try:
            validate_rq2_public_manifest(public_manifest)
        except PublicReportError as exc:
            raise RQ2PublicExportError(str(exc)) from exc
        _write_json(staged / "run-manifest.json", public_manifest)
        _privacy_scan(staged)
        if {path.name for path in staged.iterdir()} != sibling_names | {
            "run-manifest.json"
        }:
            raise RQ2PublicExportError("public artifact allowlist changed")
        _publish_directory(staged, output)
        moved = True
        return output
    finally:
        if not moved and staged.exists():
            shutil.rmtree(staged)


__all__ = [
    "DEFAULT_CONFIG",
    "DEFAULT_OUTPUT",
    "RQ2PublicExportError",
    "export_rq2_public_report",
]
