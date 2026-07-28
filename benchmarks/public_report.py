"""Allowlist-only conversion and validation for public benchmark records."""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping, Sequence


_PUBLIC_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RQ2_STAGE_COUNTS = {
    "pdf-chunker": 7,
    "note-chunker": 4,
    "retriever": 3,
    "source-composition": 5,
    "reranker": 4,
    "top2-confirmation": 12,
}
_RQ2_ARTIFACT_NAMES = {
    "morning-report.md",
    "leaderboard.csv",
    "paper-domain-breakdown.csv",
    "paired-bootstrap.json",
    "pareto-frontier.json",
    "blocked-and-unmapped.jsonl",
}


class PublicReportError(ValueError):
    """Raised when an internal hit cannot be published safely."""


def _required_id(hit: Mapping[str, object], field: str) -> str:
    value = hit.get(field)
    if not isinstance(value, str) or not _PUBLIC_ID.fullmatch(value):
        raise PublicReportError(f"{field} is missing or is not a safe public id")
    return value


def _optional_id(hit: Mapping[str, object], field: str) -> str | None:
    value = hit.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not _PUBLIC_ID.fullmatch(value):
        raise PublicReportError(f"{field} is not a safe public id")
    return value


def _page_index(hit: Mapping[str, object]) -> int | None:
    value = hit.get("pdf_page_index")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicReportError("pdf_page_index must be a non-negative integer")
    return value


def _required_bool(row: Mapping[str, object], field: str) -> bool:
    value = row.get(field)
    if not isinstance(value, bool):
        raise PublicReportError(f"{field} must be a boolean")
    return value


def _required_mapping(
    value: object,
    field: str,
) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise PublicReportError(f"{field} must be an object")
    return value


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PublicReportError(f"{field} must be a finite number")
    number = float(value)
    if not (-float("inf") < number < float("inf")):
        raise PublicReportError(f"{field} must be a finite number")
    return number


def _safe_fingerprint(value: object, field: str) -> str:
    if not isinstance(value, str) or not _PUBLIC_ID.fullmatch(value):
        raise PublicReportError(f"{field} fingerprint is unsafe or missing")
    return value


@dataclass(frozen=True)
class PublicHit:
    """The only retrieval-hit fields permitted in a public artifact."""

    paper_id: str
    file_id: str
    pdf_page_index: int | None
    evidence_id: str | None

    @classmethod
    def from_internal(cls, hit: Mapping[str, object]) -> "PublicHit":
        return cls(
            paper_id=_required_id(hit, "paper_id"),
            file_id=_required_id(hit, "file_id"),
            pdf_page_index=_page_index(hit),
            evidence_id=_optional_id(hit, "evidence_id"),
        )


def sanitize_hits(
    internal_hits: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Drop paths, source text, private queries, secrets, and model internals."""
    return [asdict(PublicHit.from_internal(hit)) for hit in internal_hits]


def sanitize_rq2_blocked_rows(
    rows: Iterable[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Rewrite internal blocked/unmapped rows through kind-specific allowlists."""

    sanitized: list[dict[str, object]] = []
    for row in rows:
        kind = row.get("kind")
        if kind == "candidate":
            status = _required_id(row, "status")
            if status not in {"failed", "incomplete", "completed"}:
                raise PublicReportError("candidate status is not terminal")
            sanitized.append(
                {
                    "kind": kind,
                    "stage_id": _required_id(row, "stage_id"),
                    "config_id": _required_id(row, "config_id"),
                    "status": status,
                    "rankable": _required_bool(row, "rankable"),
                    "mapping_passed": _required_bool(row, "mapping_passed"),
                    "guardrails_passed": _required_bool(
                        row, "guardrails_passed"
                    ),
                }
            )
        elif kind == "unmapped-evidence":
            sanitized.append(
                {
                    "kind": kind,
                    "stage_id": _required_id(row, "stage_id"),
                    "config_id": _required_id(row, "config_id"),
                    "row_id": _required_id(row, "row_id"),
                    "paper_id": _required_id(row, "paper_id"),
                    "group_id": _required_id(row, "group_id"),
                }
            )
        else:
            raise PublicReportError("blocked row kind is unsupported")
    return sanitized


def _validate_rq2_mapping(value: object) -> None:
    mapping = _required_mapping(value, "mapping")
    if mapping.get("passed") is not True:
        raise PublicReportError("mapping gate did not pass")
    mapped = mapping.get("mapped_groups")
    total = mapping.get("total_groups")
    if (
        isinstance(mapped, bool)
        or not isinstance(mapped, int)
        or isinstance(total, bool)
        or not isinstance(total, int)
        or total <= 0
        or mapped < 0
        or mapped > total
    ):
        raise PublicReportError("mapping group counts are invalid")
    overall = _finite_number(mapping.get("overall"), "mapping overall")
    if abs(overall - (mapped / total)) > 1e-12 or overall < 0.95:
        raise PublicReportError("mapping overall coverage is inconsistent")
    per_paper = _required_mapping(mapping.get("per_paper"), "mapping per_paper")
    if len(per_paper) != 20:
        raise PublicReportError("mapping must cover exactly 20 papers")
    for paper_id, score in per_paper.items():
        _required_id({"paper_id": paper_id}, "paper_id")
        if _finite_number(score, f"mapping {paper_id}") < 0.90:
            raise PublicReportError("mapping per-paper coverage is below gate")


def _validate_rq2_candidates(
    value: object,
) -> tuple[set[str], dict[str, Mapping[str, object]]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PublicReportError("candidates must be an array")
    if len(value) != sum(_RQ2_STAGE_COUNTS.values()):
        raise PublicReportError("candidates must contain 35 unique records")
    stage_counts = {stage_id: 0 for stage_id in _RQ2_STAGE_COUNTS}
    by_id: dict[str, Mapping[str, object]] = {}
    for item in value:
        candidate = _required_mapping(item, "candidate")
        config_id = _required_id(candidate, "config_id")
        stage_id = _required_id(candidate, "stage_id")
        if stage_id not in stage_counts or config_id in by_id:
            raise PublicReportError("candidates contain an unknown stage or duplicate")
        status = _required_id(candidate, "status")
        if status not in {"completed", "failed"}:
            raise PublicReportError("candidates contain a non-terminal status")
        _required_bool(candidate, "rankable")
        _required_bool(candidate, "mapping_passed")
        _required_bool(candidate, "guardrails_passed")
        stage_counts[stage_id] += 1
        by_id[config_id] = candidate
    if stage_counts != _RQ2_STAGE_COUNTS:
        raise PublicReportError("candidates have incorrect stage counts")
    return set(by_id), by_id


def _validate_rq2_confirmation(value: object) -> None:
    confirmation = _required_mapping(value, "confirmation")
    expected = {
        "cartesian_rows": 16,
        "unique_candidates": 12,
        "deduplicated_aliases": 4,
        "compatibility_rule": "hierarchical-pdf-requires-pdf-parent-child",
    }
    if dict(confirmation) != expected:
        raise PublicReportError("confirmation plan is inconsistent")


def _validate_rq2_bootstrap(
    value: object,
    candidate_ids: set[str],
) -> Mapping[str, object]:
    bootstrap = _required_mapping(value, "bootstrap")
    samples = bootstrap.get("samples")
    confidence = _finite_number(bootstrap.get("confidence"), "bootstrap confidence")
    if (
        isinstance(samples, bool)
        or not isinstance(samples, int)
        or samples < 10_000
        or abs(confidence - 0.95) > 1e-12
    ):
        raise PublicReportError("bootstrap configuration is incomplete")
    for field in ("candidate_config_id", "baseline_config_id"):
        config_id = _required_id(bootstrap, field)
        if config_id not in candidate_ids:
            raise PublicReportError("bootstrap references an unknown candidate")
    lower = _finite_number(bootstrap.get("lower"), "bootstrap lower")
    observed = _finite_number(
        bootstrap.get("observed_delta"), "bootstrap observed_delta"
    )
    upper = _finite_number(bootstrap.get("upper"), "bootstrap upper")
    if not lower <= observed <= upper:
        raise PublicReportError("bootstrap interval excludes its observation")
    return bootstrap


def _validate_rq2_artifacts(value: object) -> None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PublicReportError("artifacts must be an array")
    found: set[str] = set()
    for item in value:
        artifact = _required_mapping(item, "artifact")
        name = artifact.get("name")
        size = artifact.get("bytes")
        digest = artifact.get("sha256")
        if (
            not isinstance(name, str)
            or name not in _RQ2_ARTIFACT_NAMES
            or name in found
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or not _SHA256.fullmatch(digest)
        ):
            raise PublicReportError("artifact entry is unsafe or incomplete")
        found.add(name)
    if found != _RQ2_ARTIFACT_NAMES:
        raise PublicReportError("artifact set is incomplete")


def validate_rq2_public_manifest(manifest: Mapping[str, object]) -> None:
    """Fail closed unless every rq-2 publication completion gate is present."""

    if manifest.get("status") != "completed":
        raise PublicReportError("status must be completed")

    fingerprints = _required_mapping(
        manifest.get("fingerprints"), "fingerprints"
    )
    for key in ("code", "config", "embedding-model", "reranker-model", "data"):
        _safe_fingerprint(fingerprints.get(key), key)

    hardware = _required_mapping(
        manifest.get("hardware_fingerprints"), "hardware"
    )
    if set(hardware) != {"platform", "cpu", "gpu"}:
        raise PublicReportError("hardware fingerprints are incomplete")
    for key, value in hardware.items():
        _safe_fingerprint(value, f"hardware {key}")

    task_counts = _required_mapping(manifest.get("task_counts"), "task counts")
    for status in ("pending", "running", "completed", "failed", "blocked"):
        count = task_counts.get(status)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise PublicReportError("task counts are invalid")
    if (
        task_counts["pending"]
        or task_counts["running"]
        or task_counts["failed"]
        or task_counts["blocked"]
        or task_counts["completed"] < 1
    ):
        raise PublicReportError("task completion gate failed")

    _validate_rq2_mapping(manifest.get("mapping_coverage"))
    candidate_ids, candidates = _validate_rq2_candidates(
        manifest.get("candidates")
    )
    _validate_rq2_confirmation(manifest.get("confirmation_plan"))
    bootstrap = _validate_rq2_bootstrap(
        manifest.get("bootstrap"), candidate_ids
    )

    pareto = manifest.get("pareto_frontier")
    if not isinstance(pareto, Sequence) or isinstance(pareto, (str, bytes)) or not pareto:
        raise PublicReportError("pareto frontier is empty")
    pareto_ids: set[str] = set()
    for item in pareto:
        row = _required_mapping(item, "pareto row")
        config_id = _required_id(row, "config_id")
        if config_id not in candidate_ids:
            raise PublicReportError("pareto frontier references unknown candidate")
        pareto_ids.add(config_id)

    winner = _required_id(manifest, "provisional_winner")
    if (
        winner not in pareto_ids
        or candidates[winner].get("status") != "completed"
        or candidates[winner].get("stage_id") != "top2-confirmation"
    ):
        raise PublicReportError("winner is not a completed Pareto confirmation")
    if bootstrap.get("candidate_config_id") != winner:
        raise PublicReportError("bootstrap winner differs from provisional winner")

    _validate_rq2_artifacts(manifest.get("artifacts"))
    for field in ("partial", "blocked", "failed"):
        value = manifest.get(field)
        if not isinstance(value, list) or value:
            raise PublicReportError(f"{field} task list must be empty")
