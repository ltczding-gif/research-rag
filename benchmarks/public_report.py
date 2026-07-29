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
    "reconciliation.json",
}
_RQ2_EXTENSION_IDS = {"F2", "RR1", "R1", "S1"}
_RQ2_VALIDITY_CLASSES = {
    "valid-and-rankable",
    "valid-but-poor",
}
_RQ2_BASELINE_CLASS_COUNTS = {
    "valid-and-rankable": 6,
    "valid-but-poor": 26,
    "diagnostic-only/ineligible": 2,
    "deterministic-strategy-failure": 1,
    "infrastructure/unknown": 0,
    "invalid-false-score": 0,
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


def _validate_rq2_extensions(
    value: object,
) -> tuple[set[str], dict[str, Mapping[str, object]]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PublicReportError("approved_extensions must be an array")
    if len(value) != len(_RQ2_EXTENSION_IDS):
        raise PublicReportError(
            "approved_extensions must contain four terminal records"
        )
    extension_ids: set[str] = set()
    by_config: dict[str, Mapping[str, object]] = {}
    for item in value:
        row = _required_mapping(item, "approved extension")
        extension_id = _required_id(row, "extension_id")
        config_id = _required_id(row, "config_id")
        _required_id(row, "stage_id")
        _required_id(row, "baseline_config_id")
        validity_class = _required_id(row, "validity_class")
        if (
            extension_id not in _RQ2_EXTENSION_IDS
            or extension_id in extension_ids
            or config_id in by_config
            or row.get("status") != "completed"
            or validity_class not in _RQ2_VALIDITY_CLASSES
        ):
            raise PublicReportError(
                "approved extension identity/status is inconsistent"
            )
        rankable = _required_bool(row, "rankable")
        mapping_passed = _required_bool(row, "mapping_passed")
        guardrails_passed = _required_bool(row, "guardrails_passed")
        _finite_number(row.get("primary_score"), "extension primary")
        _finite_number(row.get("primary_delta"), "extension delta")
        _finite_number(row.get("p95_latency_ms"), "extension latency")
        for field in (
            "input_fingerprint",
            "payload_sha256",
            "progress_payload_sha256",
            "code_fingerprint",
        ):
            value = row.get(field)
            if not isinstance(value, str) or not _SHA256.fullmatch(value):
                raise PublicReportError(
                    f"approved extension {field} is invalid"
                )
        if (
            not rankable
            or not mapping_passed
            or (
                validity_class == "valid-and-rankable"
                and not guardrails_passed
            )
            or (
                validity_class == "valid-but-poor"
                and guardrails_passed
            )
        ):
            raise PublicReportError(
                "approved extension validity classification is inconsistent"
            )
        extension_ids.add(extension_id)
        by_config[config_id] = row
    if extension_ids != _RQ2_EXTENSION_IDS:
        raise PublicReportError("approved extension set is incomplete")
    return set(by_config), by_config


def _validate_rq2_reconciliation(
    value: object,
    *,
    task_counts: Mapping[str, object],
    extension_ids: set[str],
) -> Mapping[str, object]:
    reconciliation = _required_mapping(value, "reconciliation")
    if (
        reconciliation.get("schema_version") != 1
        or reconciliation.get("status") != "completed"
        or reconciliation.get("revision")
        != "rq2-superseding-reconciliation-v1"
        or reconciliation.get("baseline_candidate_count") != 35
        or reconciliation.get("superseded_failure_count", 0) < 1
    ):
        raise PublicReportError("reconciliation completion state is invalid")
    if (
        reconciliation.get("baseline_classification_counts")
        != _RQ2_BASELINE_CLASS_COUNTS
        or reconciliation.get("stop_after_report") is not True
        or reconciliation.get("rq5_started") is not False
    ):
        raise PublicReportError(
            "reconciliation validity classes or stop gate are invalid"
        )
    for field in (
        "payload_sha256",
        "source_run_state_sha256",
        "code_fingerprint",
        "data_inputs_fingerprint",
    ):
        digest = reconciliation.get(field)
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
            raise PublicReportError(
                f"reconciliation {field} is invalid"
            )
    approved = reconciliation.get("approved_extension_config_ids")
    if (
        not isinstance(approved, list)
        or set(map(str, approved)) != extension_ids
    ):
        raise PublicReportError(
            "reconciliation extension set is inconsistent"
        )
    effective = _required_mapping(
        reconciliation.get("effective_task_counts"),
        "reconciliation task counts",
    )
    if dict(effective) != dict(task_counts):
        raise PublicReportError(
            "reconciliation task counts differ from the manifest"
        )
    note = _required_mapping(
        reconciliation.get("note_prequality"),
        "reconciliation note pre-quality",
    )
    if (
        note.get("status") != "passed"
        or note.get("paper_count") != 20
        or note.get("eligible_paper_count") != 20
        or note.get("fallback_paper_count") != 0
        or not isinstance(note.get("artifact_sha256"), str)
        or not _SHA256.fullmatch(str(note.get("artifact_sha256")))
    ):
        raise PublicReportError(
            "reconciliation note pre-quality is incomplete"
        )
    return reconciliation


def _validate_rq2_confirmation(value: object) -> None:
    confirmation = _required_mapping(value, "confirmation")
    expected = {
        "cartesian_rows": 16,
        "unique_candidates": 12,
        "deduplicated_aliases": 4,
        "compatibility_rule": "hierarchical-pdf-requires-pdf-parent-child",
        "pdf_chunkers": ["pdf-fixed-800", "pdf-fixed-1200"],
        "retrievers": ["dense", "hybrid-rrf"],
        "source_compositions": ["pdf-only", "hierarchical-pdf"],
        "reranker_modes": ["rerank-off", "rerank-50-to-10"],
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
    if manifest.get("retrieval_scope") != "paper-scoped":
        raise PublicReportError("retrieval scope must be paper-scoped")
    if dict(_required_mapping(manifest.get("stage_anchors"), "stage anchors")) != {
        "pdf_chunker": "pdf-fixed-1200",
        "note_chunker": "note-reviewer-concern",
        "retriever": "hybrid-rrf",
        "source_composition": "pdf-only",
    }:
        raise PublicReportError("stage anchors are inconsistent")

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
    extension_ids, extensions = _validate_rq2_extensions(
        manifest.get("approved_extensions")
    )
    if candidate_ids & extension_ids:
        raise PublicReportError(
            "baseline and extension candidate IDs overlap"
        )
    if any(
        row.get("baseline_config_id") not in candidate_ids
        for row in extensions.values()
    ):
        raise PublicReportError(
            "approved extension references an unknown frozen baseline"
        )
    all_candidate_ids = candidate_ids | extension_ids
    _validate_rq2_confirmation(manifest.get("confirmation_plan"))
    bootstrap = _validate_rq2_bootstrap(
        manifest.get("bootstrap"), all_candidate_ids
    )
    reconciliation = _validate_rq2_reconciliation(
        manifest.get("reconciliation"),
        task_counts=task_counts,
        extension_ids=extension_ids,
    )

    pareto = manifest.get("pareto_frontier")
    if not isinstance(pareto, Sequence) or isinstance(pareto, (str, bytes)) or not pareto:
        raise PublicReportError("pareto frontier is empty")
    pareto_ids: set[str] = set()
    for item in pareto:
        row = _required_mapping(item, "pareto row")
        config_id = _required_id(row, "config_id")
        if config_id not in all_candidate_ids:
            raise PublicReportError("pareto frontier references unknown candidate")
        pareto_ids.add(config_id)

    winner = _required_id(manifest, "provisional_winner")
    winner_row = candidates.get(winner) or extensions.get(winner)
    baseline_winner = winner in candidates
    if (
        winner not in pareto_ids
        or winner_row is None
        or winner_row.get("status") != "completed"
        or (
            baseline_winner
            and winner_row.get("stage_id") != "top2-confirmation"
        )
        or winner_row.get("rankable") is not True
        or winner_row.get("mapping_passed") is not True
        or winner_row.get("guardrails_passed") is not True
        or (
            not baseline_winner
            and winner_row.get("validity_class")
            != "valid-and-rankable"
        )
    ):
        raise PublicReportError(
            "winner is not an eligible Pareto completion"
        )
    if bootstrap.get("candidate_config_id") != winner:
        raise PublicReportError("bootstrap winner differs from provisional winner")
    if reconciliation.get("provisional_winner") != winner:
        raise PublicReportError(
            "reconciliation winner differs from provisional winner"
        )

    _validate_rq2_artifacts(manifest.get("artifacts"))
    for field in ("partial", "blocked", "failed"):
        value = manifest.get(field)
        if not isinstance(value, list) or value:
            raise PublicReportError(f"{field} task list must be empty")
