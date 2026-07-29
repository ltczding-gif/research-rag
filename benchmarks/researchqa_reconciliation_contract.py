"""Fail-closed contract for the rq-2 superseding reconciliation.

The original outer runner state is immutable evidence: its runtime task ended
in a CUDA failure and the generic runner correctly refuses to re-run terminal
tasks.  A later repair therefore cannot rewrite that task to ``completed``.
Instead, the reconciliation binds the historical state, the audited frozen
matrix, every approved extension, and a deterministic set of aggregate report
artifacts.  Publication may use the effective completion state only after this
module verifies the whole envelope and every referenced artifact hash.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from benchmarks.overnight import fingerprint_payload, sha256_path


RECONCILIATION_SCHEMA_VERSION = 1
RECONCILIATION_REVISION = "rq2-superseding-reconciliation-v1"
RECONCILIATION_RELATIVE_PATH = (
    "sweep/final/superseding-reconciliation.json"
)
RECONCILIATION_ARTIFACT_PATHS = frozenset(
    {
        "sweep/final/superseding-decision-summary.json",
        "sweep/final/superseding-leaderboard.json",
        "sweep/final/superseding-pareto-frontier.json",
        "report/superseding-leaderboard.csv",
        "report/superseding-paper-domain-breakdown.csv",
        "report/superseding-paired-bootstrap.json",
        "report/superseding-blocked-and-unmapped.jsonl",
    }
)
APPROVED_EXTENSION_IDS = frozenset({"F2", "RR1", "R1", "S1"})
EXPECTED_BASELINE_CLASS_COUNTS = {
    "valid-and-rankable": 6,
    "valid-but-poor": 26,
    "diagnostic-only/ineligible": 2,
    "deterministic-strategy-failure": 1,
    "infrastructure/unknown": 0,
    "invalid-false-score": 0,
}
EXPECTED_BASELINE_STAGE_COUNTS = {
    "pdf-chunker": 7,
    "note-chunker": 4,
    "retriever": 3,
    "source-composition": 5,
    "reranker": 4,
    "top2-confirmation": 12,
}
EXPECTED_EFFECTIVE_TASK_IDS = frozenset(
    {
        "source-corpus",
        "frozen-baseline-matrix",
        "note-prequality",
        "extension-F2",
        "extension-RR1",
        "extension-R1",
        "extension-S1",
        "final-reconciliation",
    }
)


class ReconciliationContractError(ValueError):
    """Raised when the superseding completion evidence is incomplete."""


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ReconciliationContractError(f"{label} must be a mapping")
    return value


def _read_json_mapping(path: Path, label: str) -> Mapping[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconciliationContractError(f"{label} is unreadable") from exc
    return _mapping(value, label)


def _safe_relative_path(value: object, root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise ReconciliationContractError(
            "reconciliation artifact path must be non-empty"
        )
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts:
        raise ReconciliationContractError(
            "reconciliation artifact path must stay under the run root"
        )
    resolved = (root / relative).resolve(strict=False)
    if root != resolved and root not in resolved.parents:
        raise ReconciliationContractError(
            "reconciliation artifact path escapes the run root"
        )
    return resolved


def reconciliation_code_fingerprint() -> str:
    """Fingerprint every module that can admit or publish an rq-2 result."""

    root = Path(__file__).resolve().parent
    paths = (
        root / "researchqa_reconciliation.py",
        Path(__file__),
        root / "researchqa_runtime.py",
        root / "researchqa_strategy.py",
        root / "researchqa_retrieval.py",
        root / "researchqa_sweep.py",
        root / "researchqa_validity_audit.py",
        root / "researchqa_public_export.py",
        root / "public_report.py",
    )
    rows = []
    for path in paths:
        try:
            _size, digest = sha256_path(path)
        except OSError as exc:
            raise ReconciliationContractError(
                f"reconciliation code input is unavailable: {path.name}"
            ) from exc
        rows.append([path.name, digest])
    return fingerprint_payload(rows)


def _verify_hashed_file(
    root: Path,
    relative: str,
    expected_sha256: object,
    label: str,
) -> Path:
    if not _is_sha256(expected_sha256):
        raise ReconciliationContractError(f"{label} SHA-256 is invalid")
    path = _safe_relative_path(relative, root)
    try:
        _size, actual = sha256_path(path)
    except OSError as exc:
        raise ReconciliationContractError(f"{label} is unavailable") from exc
    if actual != expected_sha256:
        raise ReconciliationContractError(f"{label} hash mismatch")
    return path


def reconciliation_path(run_root: str | Path) -> Path:
    return (
        Path(run_root).resolve(strict=False)
        / Path(RECONCILIATION_RELATIVE_PATH)
    )


def load_rq2_reconciliation(
    run_root: str | Path,
    *,
    expected_config_fingerprint: str | None = None,
) -> Mapping[str, Any]:
    """Load and verify one completed reconciliation and all bound artifacts."""

    root = Path(run_root).resolve(strict=True)
    path = reconciliation_path(root)
    try:
        envelope = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReconciliationContractError(
            "superseding reconciliation is unreadable"
        ) from exc
    outer = _mapping(envelope, "reconciliation envelope")
    payload = _mapping(outer.get("payload"), "reconciliation payload")
    if (
        outer.get("schema_version") != RECONCILIATION_SCHEMA_VERSION
        or outer.get("revision") != RECONCILIATION_REVISION
        or outer.get("run_id") != root.name
        or outer.get("payload_sha256") != fingerprint_payload(payload)
    ):
        raise ReconciliationContractError(
            "reconciliation envelope identity or payload hash is invalid"
        )
    if (
        payload.get("status") != "completed"
        or payload.get("public_export_ready") is not True
        or payload.get("run_id") != root.name
    ):
        raise ReconciliationContractError(
            "reconciliation is not a completed publication state"
        )

    fingerprints = _mapping(
        payload.get("fingerprints"),
        "reconciliation fingerprints",
    )
    for key in (
        "code",
        "config",
        "embedding-model",
        "reranker-model",
        "data-inputs",
    ):
        if not _is_sha256(fingerprints.get(key)):
            raise ReconciliationContractError(
                f"reconciliation {key} fingerprint is invalid"
            )
    if fingerprints.get("code") != reconciliation_code_fingerprint():
        raise ReconciliationContractError(
            "reconciliation/code fingerprint differs from current code"
        )
    if (
        expected_config_fingerprint is not None
        and fingerprints.get("config") != expected_config_fingerprint
    ):
        raise ReconciliationContractError(
            "reconciliation/config fingerprint mismatch"
        )

    source_state = _mapping(
        payload.get("source_run_state"),
        "source run state",
    )
    state_path = root / "run-state.json"
    try:
        _size, state_sha256 = sha256_path(state_path)
    except OSError as exc:
        raise ReconciliationContractError(
            "historical run state is unavailable"
        ) from exc
    if (
        source_state.get("sha256") != state_sha256
        or source_state.get("status") not in {"partial", "failed"}
        or not isinstance(
            source_state.get("superseded_failed_task_ids"),
            list,
        )
        or not source_state["superseded_failed_task_ids"]
    ):
        raise ReconciliationContractError(
            "historical outer failure is not hash-bound and superseded"
        )

    baseline = _mapping(payload.get("baseline_audit"), "baseline audit")
    if (
        baseline.get("candidate_count") != 35
        or baseline.get("validity_gate_closed") is not True
        or baseline.get("classification_counts")
        != EXPECTED_BASELINE_CLASS_COUNTS
        or baseline.get("stage_counts") != EXPECTED_BASELINE_STAGE_COUNTS
        or not _is_sha256(baseline.get("rows_fingerprint"))
    ):
        raise ReconciliationContractError(
            "frozen 35-candidate validity audit is incomplete"
        )
    baseline_artifacts = baseline.get("candidate_artifacts")
    if (
        not isinstance(baseline_artifacts, list)
        or len(baseline_artifacts) != 35
    ):
        raise ReconciliationContractError(
            "frozen baseline artifact manifest is incomplete"
        )
    baseline_ids: set[str] = set()
    baseline_stage_counts = {
        stage_id: 0 for stage_id in EXPECTED_BASELINE_STAGE_COUNTS
    }
    for row_value in baseline_artifacts:
        row = _mapping(row_value, "baseline candidate artifact")
        config_id = row.get("config_id")
        stage_id = row.get("stage_id")
        if (
            not isinstance(config_id, str)
            or not config_id
            or config_id in baseline_ids
            or stage_id not in baseline_stage_counts
        ):
            raise ReconciliationContractError(
                "baseline candidate artifact identity is invalid"
            )
        relative = (
            f"sweep/candidates/{stage_id}/{config_id}.json"
        )
        candidate_path = _verify_hashed_file(
            root,
            relative,
            row.get("file_sha256"),
            f"baseline candidate {config_id}",
        )
        envelope_value = _read_json_mapping(
            candidate_path,
            f"baseline candidate {config_id}",
        )
        if (
            envelope_value.get("config_id") != config_id
            or envelope_value.get("stage_id") != stage_id
            or envelope_value.get("status") not in {"completed", "failed"}
            or envelope_value.get("payload_sha256")
            != fingerprint_payload(
                _mapping(
                    envelope_value.get("payload"),
                    f"baseline candidate {config_id} payload",
                )
            )
        ):
            raise ReconciliationContractError(
                f"baseline candidate envelope is invalid: {config_id}"
            )
        baseline_ids.add(config_id)
        baseline_stage_counts[str(stage_id)] += 1
    if baseline_stage_counts != EXPECTED_BASELINE_STAGE_COUNTS:
        raise ReconciliationContractError(
            "baseline candidate artifact stage counts are invalid"
        )

    extensions = payload.get("approved_extensions")
    if not isinstance(extensions, list) or len(extensions) != 4:
        raise ReconciliationContractError(
            "approved extension reconciliation must contain four rows"
        )
    extension_ids: set[str] = set()
    extension_config_ids: set[str] = set()
    for row_value in extensions:
        row = _mapping(row_value, "approved extension")
        extension_id = row.get("extension_id")
        config_id = row.get("config_id")
        if (
            extension_id not in APPROVED_EXTENSION_IDS
            or extension_id in extension_ids
            or not isinstance(config_id, str)
            or not config_id
            or config_id in extension_config_ids
            or row.get("status") != "completed"
            or row.get("mapping_passed") is not True
            or row.get("contract_errors") != []
            or row.get("baseline_config_id") not in baseline_ids
            or row.get("validity_class")
            not in {"valid-and-rankable", "valid-but-poor"}
        ):
            raise ReconciliationContractError(
                "approved extension row is incomplete or invalid"
            )
        for key in (
            "input_fingerprint",
            "payload_sha256",
            "progress_payload_sha256",
            "code_fingerprint",
            "candidate_file_sha256",
            "progress_file_sha256",
            "runtime_summary_sha256",
            "prequality_sha256",
            "model_preflight_sha256",
        ):
            if not _is_sha256(row.get(key)):
                raise ReconciliationContractError(
                    f"approved extension {extension_id} has invalid {key}"
                )
        stage_id = row.get("stage_id")
        input_fingerprint = row.get("input_fingerprint")
        if not isinstance(stage_id, str) or not stage_id:
            raise ReconciliationContractError(
                f"approved extension {extension_id} has invalid stage_id"
            )
        candidate_path = _verify_hashed_file(
            root,
            (
                f"sweep/extensions/{extension_id}/candidates/"
                f"{stage_id}/{config_id}.json"
            ),
            row.get("candidate_file_sha256"),
            f"{extension_id} candidate",
        )
        progress_path = _verify_hashed_file(
            root,
            (
                f"sweep/progress/{stage_id}/{config_id}/"
                f"{input_fingerprint}.json"
            ),
            row.get("progress_file_sha256"),
            f"{extension_id} progress",
        )
        for filename, field in (
            ("runtime-summary.json", "runtime_summary_sha256"),
            ("prequality.json", "prequality_sha256"),
            ("model-preflight.json", "model_preflight_sha256"),
        ):
            _verify_hashed_file(
                root,
                f"sweep/extensions/{extension_id}/runtime/{filename}",
                row.get(field),
                f"{extension_id} {filename}",
            )
        candidate_envelope = _read_json_mapping(
            candidate_path,
            f"{extension_id} candidate",
        )
        candidate_payload = _mapping(
            candidate_envelope.get("payload"),
            f"{extension_id} candidate payload",
        )
        progress_envelope = _read_json_mapping(
            progress_path,
            f"{extension_id} progress",
        )
        progress_payload = _mapping(
            progress_envelope.get("payload"),
            f"{extension_id} progress payload",
        )
        if (
            candidate_envelope.get("config_id") != config_id
            or candidate_envelope.get("stage_id") != stage_id
            or candidate_envelope.get("input_fingerprint")
            != input_fingerprint
            or candidate_envelope.get("status") != "completed"
            or candidate_envelope.get("payload_sha256")
            != row.get("payload_sha256")
            or candidate_envelope.get("payload_sha256")
            != fingerprint_payload(candidate_payload)
            or progress_envelope.get("config_id") != config_id
            or progress_envelope.get("stage_id") != stage_id
            or progress_envelope.get("input_fingerprint")
            != input_fingerprint
            or progress_envelope.get("payload_sha256")
            != row.get("progress_payload_sha256")
            or progress_envelope.get("payload_sha256")
            != fingerprint_payload(progress_payload)
            or progress_payload.get("code_fingerprint")
            != row.get("code_fingerprint")
        ):
            raise ReconciliationContractError(
                f"approved extension {extension_id} evidence differs "
                "from reconciliation"
            )
        extension_ids.add(str(extension_id))
        extension_config_ids.add(config_id)
    if extension_ids != APPROVED_EXTENSION_IDS:
        raise ReconciliationContractError(
            "approved extension set is incomplete"
        )

    note_prequality = _mapping(
        payload.get("note_prequality"),
        "note pre-quality",
    )
    if (
        note_prequality.get("status") != "passed"
        or note_prequality.get("paper_count") != 20
        or note_prequality.get("eligible_paper_count") != 20
        or note_prequality.get("fallback_paper_count") != 0
        or note_prequality.get("backlinkable_base_chunk_count")
        != note_prequality.get("base_chunk_count")
        or note_prequality.get("backlinkable_reviewer_chunk_count")
        != note_prequality.get("reviewer_chunk_count")
        or not _is_sha256(note_prequality.get("diagnostic_fingerprint"))
        or not _is_sha256(note_prequality.get("artifact_sha256"))
    ):
        raise ReconciliationContractError(
            "N0/N3/N1 pre-quality evidence is incomplete"
        )
    _verify_hashed_file(
        root,
        "sweep/extensions/N0-N3/runtime/prequality.json",
        note_prequality.get("artifact_sha256"),
        "N0/N3 pre-quality",
    )

    tasks = payload.get("effective_tasks")
    if not isinstance(tasks, list) or len(tasks) != len(
        EXPECTED_EFFECTIVE_TASK_IDS
    ):
        raise ReconciliationContractError(
            "effective reconciliation task list is incomplete"
        )
    task_ids = {
        str(_mapping(row, "effective task").get("task_id"))
        for row in tasks
    }
    if task_ids != EXPECTED_EFFECTIVE_TASK_IDS or any(
        _mapping(row, "effective task").get("status") != "completed"
        for row in tasks
    ):
        raise ReconciliationContractError(
            "effective reconciliation tasks are not all completed"
        )
    expected_counts = {
        "pending": 0,
        "running": 0,
        "completed": len(EXPECTED_EFFECTIVE_TASK_IDS),
        "failed": 0,
        "blocked": 0,
    }
    if payload.get("effective_task_counts") != expected_counts:
        raise ReconciliationContractError(
            "effective reconciliation task counts are invalid"
        )

    decision = _mapping(payload.get("decision"), "reconciliation decision")
    winner_id = decision.get("provisional_winner")
    winner = next(
        (
            row
            for row in extensions
            if isinstance(row, Mapping) and row.get("config_id") == winner_id
        ),
        None,
    )
    if (
        not isinstance(winner, Mapping)
        or winner.get("validity_class") != "valid-and-rankable"
        or winner.get("guardrails_passed") is not True
        or decision.get("baseline_config_id")
        != winner.get("baseline_config_id")
        or decision.get("primary_metric") != "coverage_ndcg_at_10"
        or decision.get("primary_score") != winner.get("primary_score")
        or decision.get("primary_delta") != winner.get("primary_delta")
        or decision.get("guardrails_passed") is not True
        or decision.get("stop_after_report") is not True
        or decision.get("rq5_started") is not False
        or not isinstance(decision.get("pareto_config_ids"), list)
        or winner_id not in decision["pareto_config_ids"]
    ):
        raise ReconciliationContractError(
            "reconciliation winner decision is inconsistent"
        )

    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise ReconciliationContractError(
            "reconciliation artifacts must be a list"
        )
    found_paths: set[str] = set()
    for row_value in artifacts:
        row = _mapping(row_value, "reconciliation artifact")
        relative = row.get("path")
        if (
            not isinstance(relative, str)
            or relative in found_paths
            or relative not in RECONCILIATION_ARTIFACT_PATHS
            or not isinstance(row.get("bytes"), int)
            or row["bytes"] < 0
            or not _is_sha256(row.get("sha256"))
        ):
            raise ReconciliationContractError(
                "reconciliation artifact manifest is invalid"
            )
        artifact_path = _safe_relative_path(relative, root)
        try:
            size, digest = sha256_path(artifact_path)
        except OSError as exc:
            raise ReconciliationContractError(
                f"reconciliation artifact is unavailable: {relative}"
            ) from exc
        if size != row["bytes"] or digest != row["sha256"]:
            raise ReconciliationContractError(
                f"reconciliation artifact hash mismatch: {relative}"
            )
        found_paths.add(relative)
    if found_paths != RECONCILIATION_ARTIFACT_PATHS:
        raise ReconciliationContractError(
            "reconciliation artifact set is incomplete"
        )
    decision_artifact = _read_json_mapping(
        root / "sweep/final/superseding-decision-summary.json",
        "superseding decision summary",
    )
    pareto_artifact = _read_json_mapping(
        root / "sweep/final/superseding-pareto-frontier.json",
        "superseding Pareto frontier",
    )
    pareto_rows = pareto_artifact.get("rows")
    pareto_ids = (
        [
            str(_mapping(row, "Pareto row").get("config_id"))
            for row in pareto_rows
        ]
        if isinstance(pareto_rows, list)
        else []
    )
    if (
        decision_artifact.get("provisional_winner")
        != decision.get("provisional_winner")
        or decision_artifact.get("baseline_config_id")
        != decision.get("baseline_config_id")
        or decision_artifact.get("primary_metric")
        != decision.get("primary_metric")
        or decision_artifact.get("primary")
        != decision.get("primary_score")
        or decision_artifact.get("primary_delta")
        != decision.get("primary_delta")
        or decision_artifact.get("guardrails_passed") is not True
        or decision_artifact.get("stop_after_report") is not True
        or decision_artifact.get("rq5_started") is not False
        or pareto_ids != decision.get("pareto_config_ids")
    ):
        raise ReconciliationContractError(
            "aggregate decision artifacts differ from reconciliation"
        )
    return dict(payload)


__all__ = [
    "APPROVED_EXTENSION_IDS",
    "EXPECTED_BASELINE_CLASS_COUNTS",
    "EXPECTED_BASELINE_STAGE_COUNTS",
    "EXPECTED_EFFECTIVE_TASK_IDS",
    "RECONCILIATION_ARTIFACT_PATHS",
    "RECONCILIATION_RELATIVE_PATH",
    "RECONCILIATION_REVISION",
    "RECONCILIATION_SCHEMA_VERSION",
    "ReconciliationContractError",
    "load_rq2_reconciliation",
    "reconciliation_code_fingerprint",
    "reconciliation_path",
]
