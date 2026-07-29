"""Recoverable, hash-verified orchestration primitives for ResearchQA rq-2.

The module is intentionally adapter-driven.  Source preparation, note
generation, evidence mapping, chunking, retrieval, and reranking remain in
their owning modules; an overnight adapter supplies callbacks that this runner
executes under a deterministic task/state/checkpoint contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from statistics import fmean
from typing import Any, Callable, Iterable, Mapping, Protocol, Sequence


SCHEMA_VERSION = 1
DEFAULT_BUDGET_SECONDS = 10 * 60 * 60
DEFAULT_RETRY_DELAYS = (5.0, 20.0, 60.0)
STAGE_ORDER = (
    "sources",
    "notes",
    "evidence-map",
    "pdf-chunker",
    "note-chunker",
    "retriever",
    "source-composition",
    "reranker",
    "top2-confirmation",
    "report",
)


class OvernightError(RuntimeError):
    """Base error for deterministic overnight runner failures."""


class StateContractError(OvernightError):
    """Raised when a persisted run state violates the state contract."""


class FingerprintMismatchError(StateContractError):
    """Raised when resume fingerprints differ from the original run."""


class ArtifactIntegrityError(StateContractError):
    """Raised when a completed artifact is missing or has changed."""


class BudgetExhaustedError(OvernightError):
    """Raised before starting work that cannot fit the remaining budget."""


class TransientTaskError(OvernightError):
    """An adapter failure eligible for the configured bounded retry policy."""


class DeterministicTaskError(OvernightError):
    """A schema, citation, mapping, or hash failure that must not be retried."""


class BlockedTaskError(OvernightError):
    """A real external blocker that should mark the atomic task blocked."""


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"
    BLOCKED = "blocked"
    BUDGET_EXHAUSTED = "budget-exhausted"
    INTERRUPTED = "interrupted"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
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


def fingerprint_payload(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_path(path: str | Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    with Path(path).open("rb") as handle:
        while block := handle.read(1024 * 1024):
            digest.update(block)
            total += len(block)
    return total, digest.hexdigest()


def atomic_task_id(
    *,
    run_id: str,
    stage_id: str,
    paper_id: str,
    config_id: str,
    input_fingerprint: str,
) -> str:
    """Build the immutable task ID from all approved identity components."""

    components = (run_id, stage_id, paper_id, config_id, input_fingerprint)
    if not all(isinstance(value, str) and value for value in components):
        raise StateContractError(
            "atomic task identity components must be non-empty strings"
        )
    digest = hashlib.sha256("\0".join(components).encode("utf-8")).hexdigest()
    return f"{stage_id}-{digest[:24]}"


@dataclass(frozen=True)
class ArtifactRecord:
    path: str
    sha256: str
    bytes: int
    media_type: str = "application/octet-stream"
    schema_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "bytes": self.bytes,
            "media_type": self.media_type,
            "schema_id": self.schema_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ArtifactRecord":
        return cls(
            path=str(value["path"]),
            sha256=str(value["sha256"]),
            bytes=int(value["bytes"]),
            media_type=str(
                value.get("media_type", "application/octet-stream")
            ),
            schema_id=(
                str(value["schema_id"])
                if value.get("schema_id") is not None
                else None
            ),
        )


@dataclass(frozen=True)
class TaskSpec:
    stage_id: str
    paper_id: str
    config_id: str
    input_fingerprint: str
    estimated_atom_count: int = 1
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def task_id(self, run_id: str) -> str:
        return atomic_task_id(
            run_id=run_id,
            stage_id=self.stage_id,
            paper_id=self.paper_id,
            config_id=self.config_id,
            input_fingerprint=self.input_fingerprint,
        )


@dataclass
class TaskRecord:
    task_id: str
    stage_id: str
    paper_id: str
    config_id: str
    input_fingerprint: str
    status: TaskStatus = TaskStatus.PENDING
    attempts: int = 0
    interruptions: int = 0
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    duration_seconds: float | None = None
    artifacts: list[ArtifactRecord] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def transition(self, status: TaskStatus) -> None:
        allowed = {
            TaskStatus.PENDING: {TaskStatus.RUNNING},
            TaskStatus.RUNNING: {
                TaskStatus.COMPLETED,
                TaskStatus.FAILED,
                TaskStatus.BLOCKED,
            },
            TaskStatus.COMPLETED: set(),
            TaskStatus.FAILED: set(),
            TaskStatus.BLOCKED: set(),
        }
        if status not in allowed[self.status]:
            raise StateContractError(
                f"invalid task transition {self.status.value} -> {status.value} "
                f"for {self.task_id}"
            )
        self.status = status
        if status is TaskStatus.RUNNING:
            self.started_at = utc_now()
            self.finished_at = None
            self.error = None
        elif status in {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.BLOCKED,
        }:
            self.finished_at = utc_now()

    def recover_interrupted(self) -> None:
        """Reset an interrupted running task so the same atomic ID can resume."""

        if self.status is not TaskStatus.RUNNING:
            raise StateContractError(
                f"only running tasks can be recovered: {self.task_id}"
            )
        self.status = TaskStatus.PENDING
        self.interruptions += 1
        self.started_at = None
        self.finished_at = None
        self.duration_seconds = None
        self.artifacts = []
        self.error = "interrupted before an atomic completion checkpoint"

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "stage_id": self.stage_id,
            "paper_id": self.paper_id,
            "config_id": self.config_id,
            "input_fingerprint": self.input_fingerprint,
            "status": self.status.value,
            "attempts": self.attempts,
            "interruptions": self.interruptions,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": self.duration_seconds,
            "artifacts": [artifact.to_dict() for artifact in self.artifacts],
            "metadata": self.metadata,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskRecord":
        return cls(
            task_id=str(value["task_id"]),
            stage_id=str(value["stage_id"]),
            paper_id=str(value["paper_id"]),
            config_id=str(value["config_id"]),
            input_fingerprint=str(value["input_fingerprint"]),
            status=TaskStatus(str(value["status"])),
            attempts=int(value.get("attempts", 0)),
            interruptions=int(value.get("interruptions", 0)),
            created_at=str(value["created_at"]),
            started_at=(
                str(value["started_at"])
                if value.get("started_at") is not None
                else None
            ),
            finished_at=(
                str(value["finished_at"])
                if value.get("finished_at") is not None
                else None
            ),
            duration_seconds=(
                float(value["duration_seconds"])
                if value.get("duration_seconds") is not None
                else None
            ),
            artifacts=[
                ArtifactRecord.from_dict(item)
                for item in value.get("artifacts", [])
            ],
            metadata=dict(value.get("metadata", {})),
            error=(
                str(value["error"]) if value.get("error") is not None else None
            ),
        )


@dataclass
class StageStatistics:
    completed_count: int = 0
    total_duration_seconds: float = 0.0
    recent_durations_seconds: list[float] = field(default_factory=list)

    @property
    def moving_average_seconds(self) -> float | None:
        if not self.recent_durations_seconds:
            return None
        return fmean(self.recent_durations_seconds)

    def add(self, duration_seconds: float, *, window: int = 8) -> None:
        if duration_seconds < 0:
            raise StateContractError("task duration must be non-negative")
        self.completed_count += 1
        self.total_duration_seconds += duration_seconds
        self.recent_durations_seconds.append(duration_seconds)
        if len(self.recent_durations_seconds) > window:
            del self.recent_durations_seconds[:-window]

    def to_dict(self) -> dict[str, Any]:
        return {
            "completed_count": self.completed_count,
            "total_duration_seconds": self.total_duration_seconds,
            "recent_durations_seconds": self.recent_durations_seconds,
            "moving_average_seconds": self.moving_average_seconds,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StageStatistics":
        return cls(
            completed_count=int(value.get("completed_count", 0)),
            total_duration_seconds=float(
                value.get("total_duration_seconds", 0.0)
            ),
            recent_durations_seconds=[
                float(item)
                for item in value.get("recent_durations_seconds", [])
            ],
        )


@dataclass
class RunState:
    run_id: str
    fingerprints: dict[str, str]
    budget_seconds: float = DEFAULT_BUDGET_SECONDS
    status: RunStatus = RunStatus.PENDING
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    elapsed_seconds: float = 0.0
    tasks: dict[str, TaskRecord] = field(default_factory=dict)
    stage_statistics: dict[str, StageStatistics] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    report_artifacts: list[ArtifactRecord] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION

    def add_event(self, kind: str, **details: Any) -> None:
        self.events.append({"at": utc_now(), "kind": kind, **details})
        # Keep state bounded for long runs while retaining recent provenance.
        if len(self.events) > 5_000:
            del self.events[: len(self.events) - 5_000]

    def ensure_task(self, spec: TaskSpec) -> TaskRecord:
        task_id = spec.task_id(self.run_id)
        existing = self.tasks.get(task_id)
        if existing is not None:
            identity = (
                existing.stage_id,
                existing.paper_id,
                existing.config_id,
                existing.input_fingerprint,
            )
            expected = (
                spec.stage_id,
                spec.paper_id,
                spec.config_id,
                spec.input_fingerprint,
            )
            if identity != expected:
                raise StateContractError(
                    f"task ID collision for {task_id}: {identity} != {expected}"
                )
            return existing
        record = TaskRecord(
            task_id=task_id,
            stage_id=spec.stage_id,
            paper_id=spec.paper_id,
            config_id=spec.config_id,
            input_fingerprint=spec.input_fingerprint,
            metadata=dict(spec.metadata),
        )
        self.tasks[task_id] = record
        self.add_event("task-registered", task_id=task_id)
        return record

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "run_id": self.run_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "budget_seconds": self.budget_seconds,
            "elapsed_seconds": self.elapsed_seconds,
            "fingerprints": dict(sorted(self.fingerprints.items())),
            "tasks": {
                task_id: task.to_dict()
                for task_id, task in sorted(self.tasks.items())
            },
            "stage_statistics": {
                stage_id: stats.to_dict()
                for stage_id, stats in sorted(self.stage_statistics.items())
            },
            "events": self.events,
            "report_artifacts": [
                artifact.to_dict() for artifact in self.report_artifacts
            ],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RunState":
        if int(value.get("schema_version", -1)) != SCHEMA_VERSION:
            raise StateContractError(
                f"unsupported run-state schema_version "
                f"{value.get('schema_version')!r}"
            )
        return cls(
            schema_version=SCHEMA_VERSION,
            run_id=str(value["run_id"]),
            status=RunStatus(str(value["status"])),
            created_at=str(value["created_at"]),
            updated_at=str(value["updated_at"]),
            budget_seconds=float(value["budget_seconds"]),
            elapsed_seconds=float(value.get("elapsed_seconds", 0.0)),
            fingerprints={
                str(key): str(item)
                for key, item in dict(value["fingerprints"]).items()
            },
            tasks={
                str(task_id): TaskRecord.from_dict(task)
                for task_id, task in dict(value.get("tasks", {})).items()
            },
            stage_statistics={
                str(stage_id): StageStatistics.from_dict(stats)
                for stage_id, stats in dict(
                    value.get("stage_statistics", {})
                ).items()
            },
            events=[dict(event) for event in value.get("events", [])],
            report_artifacts=[
                ArtifactRecord.from_dict(item)
                for item in value.get("report_artifacts", [])
            ],
        )


class RunStore:
    """Own one run directory and atomically checkpoint state and artifacts."""

    def __init__(self, run_root: str | Path):
        self.root = Path(run_root).resolve(strict=False)
        self.state_path = self.root / "run-state.json"

    def require_owned(self, path: str | Path) -> Path:
        candidate = Path(path).resolve(strict=False)
        if candidate != self.root and self.root not in candidate.parents:
            raise StateContractError(
                f"refusing path outside overnight run root: {candidate}"
            )
        return candidate

    def _atomic_write(
        self,
        path: Path,
        payload: bytes,
        *,
        validator: Callable[[Path], None] | None = None,
    ) -> None:
        path = self.require_owned(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, raw_temp = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temp_path = Path(raw_temp)
        try:
            with os.fdopen(file_descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if validator is not None:
                validator(temp_path)
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    def save(self, state: RunState) -> None:
        state.updated_at = utc_now()
        self._atomic_write(self.state_path, canonical_json_bytes(state.to_dict()))

    def create(
        self,
        *,
        run_id: str,
        fingerprints: Mapping[str, str],
        budget_seconds: float = DEFAULT_BUDGET_SECONDS,
    ) -> RunState:
        if self.state_path.exists():
            raise StateContractError(
                f"run state already exists: {self.state_path}"
            )
        if budget_seconds <= 0:
            raise StateContractError("budget_seconds must be positive")
        if (
            not isinstance(run_id, str)
            or not run_id
            or len(run_id) > 128
            or not run_id[0].isalnum()
            or any(
                not (character.islower() or character.isdigit())
                and character not in "._-"
                for character in run_id
            )
        ):
            raise StateContractError(
                "run_id must match ^[a-z0-9][a-z0-9._-]{0,127}$"
            )
        if not fingerprints or not all(
            isinstance(key, str)
            and key
            and isinstance(value, str)
            and value
            for key, value in fingerprints.items()
        ):
            raise StateContractError(
                "fingerprints must be a non-empty string mapping"
            )
        state = RunState(
            run_id=run_id,
            fingerprints=dict(fingerprints),
            budget_seconds=float(budget_seconds),
        )
        state.add_event("run-created")
        self.save(state)
        return state

    def load(
        self,
        *,
        expected_fingerprints: Mapping[str, str] | None = None,
        verify_artifacts: bool = True,
        recover_running: bool = True,
    ) -> RunState:
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise StateContractError(
                f"run state does not exist: {self.state_path}"
            ) from exc
        except json.JSONDecodeError as exc:
            raise StateContractError(
                f"invalid run-state JSON: {exc.msg}"
            ) from exc
        state = RunState.from_dict(payload)
        if expected_fingerprints is not None:
            for name, expected in expected_fingerprints.items():
                actual = state.fingerprints.get(name)
                if actual != expected:
                    raise FingerprintMismatchError(
                        f"fingerprint mismatch for {name!r}: "
                        f"expected {expected}, found {actual}"
                    )
        if verify_artifacts:
            self.verify_completed_artifacts(state)
        recovered: list[str] = []
        if recover_running:
            for task in state.tasks.values():
                if task.status is TaskStatus.RUNNING:
                    task.recover_interrupted()
                    recovered.append(task.task_id)
            if recovered:
                state.status = RunStatus.INTERRUPTED
                state.add_event(
                    "interrupted-tasks-recovered", task_ids=sorted(recovered)
                )
                self.save(state)
        return state

    def artifact_record(
        self,
        path: str | Path,
        *,
        media_type: str = "application/octet-stream",
        schema_id: str | None = None,
    ) -> ArtifactRecord:
        resolved = self.require_owned(path)
        if not resolved.is_file():
            raise ArtifactIntegrityError(f"artifact does not exist: {resolved}")
        size, digest = sha256_path(resolved)
        return ArtifactRecord(
            path=resolved.relative_to(self.root).as_posix(),
            sha256=digest,
            bytes=size,
            media_type=media_type,
            schema_id=schema_id,
        )

    def write_bytes_artifact(
        self,
        relative_path: str | Path,
        payload: bytes,
        *,
        media_type: str = "application/octet-stream",
        schema_id: str | None = None,
        validator: Callable[[Path], None] | None = None,
    ) -> ArtifactRecord:
        destination = self.require_owned(self.root / relative_path)
        self._atomic_write(destination, payload, validator=validator)
        return self.artifact_record(
            destination,
            media_type=media_type,
            schema_id=schema_id,
        )

    def write_json_artifact(
        self,
        relative_path: str | Path,
        value: Any,
        *,
        schema_id: str | None = None,
        validator: Callable[[Path], None] | None = None,
    ) -> ArtifactRecord:
        return self.write_bytes_artifact(
            relative_path,
            canonical_json_bytes(value),
            media_type="application/json",
            schema_id=schema_id,
            validator=validator,
        )

    def verify_artifact(self, artifact: ArtifactRecord) -> Path:
        path = self.require_owned(self.root / artifact.path)
        if not path.is_file():
            raise ArtifactIntegrityError(f"completed artifact is missing: {path}")
        size, digest = sha256_path(path)
        if size != artifact.bytes or digest != artifact.sha256:
            raise ArtifactIntegrityError(
                f"completed artifact hash mismatch: {path}"
            )
        return path

    def verify_completed_artifacts(self, state: RunState) -> None:
        for task in state.tasks.values():
            if task.status is TaskStatus.COMPLETED:
                for artifact in task.artifacts:
                    self.verify_artifact(artifact)
        for artifact in state.report_artifacts:
            self.verify_artifact(artifact)


@dataclass(frozen=True)
class TaskOutput:
    artifacts: tuple[ArtifactRecord, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class TaskContext:
    state: RunState
    store: RunStore
    spec: TaskSpec
    task: TaskRecord


TaskCallback = Callable[[TaskContext], TaskOutput | None]
ReportCallback = Callable[[RunState, RunStore], Sequence[ArtifactRecord]]


class OvernightAdapter(Protocol):
    """Narrow adapter boundary implemented by source/note/retrieval owners."""

    def task_specs(
        self,
        command: str,
        state: RunState,
    ) -> Iterable[TaskSpec]:
        ...

    def run_task(self, context: TaskContext) -> TaskOutput | None:
        ...

    def write_report(
        self,
        state: RunState,
        store: RunStore,
    ) -> Sequence[ArtifactRecord]:
        ...


class BudgetController:
    """Wall-clock budget with per-stage moving-average admission control."""

    def __init__(
        self,
        state: RunState,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.state = state
        self._monotonic = monotonic
        self._session_started = monotonic()
        self._base_elapsed = state.elapsed_seconds

    @property
    def elapsed_seconds(self) -> float:
        return self._base_elapsed + (
            self._monotonic() - self._session_started
        )

    @property
    def remaining_seconds(self) -> float:
        return max(0.0, self.state.budget_seconds - self.elapsed_seconds)

    def checkpoint_elapsed(self) -> None:
        self.state.elapsed_seconds = self.elapsed_seconds
        self._base_elapsed = self.state.elapsed_seconds
        self._session_started = self._monotonic()

    def estimate_candidate_seconds(
        self,
        stage_id: str,
        *,
        atom_count: int,
        default_atom_seconds: float | None = None,
    ) -> float | None:
        if atom_count < 1:
            raise StateContractError("atom_count must be at least 1")
        stats = self.state.stage_statistics.get(stage_id)
        average = stats.moving_average_seconds if stats is not None else None
        if average is None:
            average = default_atom_seconds
        if average is None:
            return None
        if average < 0:
            raise StateContractError("estimated duration must be non-negative")
        return average * atom_count

    def can_start_candidate(
        self,
        stage_id: str,
        *,
        atom_count: int,
        default_atom_seconds: float | None = None,
    ) -> bool:
        estimate = self.estimate_candidate_seconds(
            stage_id,
            atom_count=atom_count,
            default_atom_seconds=default_atom_seconds,
        )
        if estimate is None:
            # A canary/first task may establish the moving average.
            return self.remaining_seconds > 0
        return self.remaining_seconds >= estimate


class OvernightRunner:
    """Execute adapter callbacks with atomic checkpoints and bounded retries."""

    def __init__(
        self,
        *,
        state: RunState,
        store: RunStore,
        retry_delays: Sequence[float] = DEFAULT_RETRY_DELAYS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if any(delay < 0 for delay in retry_delays):
            raise StateContractError("retry delays must be non-negative")
        self.state = state
        self.store = store
        self.retry_delays = tuple(float(delay) for delay in retry_delays)
        self.sleep = sleep
        self.monotonic = monotonic
        self.budget = BudgetController(state, monotonic=monotonic)

    def checkpoint(self) -> None:
        self.budget.checkpoint_elapsed()
        self.store.save(self.state)

    def _record_duration(self, stage_id: str, duration_seconds: float) -> None:
        stats = self.state.stage_statistics.setdefault(
            stage_id, StageStatistics()
        )
        stats.add(duration_seconds)

    def run_task(
        self,
        spec: TaskSpec,
        callback: TaskCallback,
        *,
        default_atom_seconds: float | None = None,
    ) -> TaskRecord:
        task = self.state.ensure_task(spec)
        if task.status is TaskStatus.COMPLETED:
            for artifact in task.artifacts:
                self.store.verify_artifact(artifact)
            return task
        if task.status in {TaskStatus.FAILED, TaskStatus.BLOCKED}:
            raise StateContractError(
                f"terminal task cannot be re-run: {task.task_id} "
                f"({task.status.value})"
            )
        if not self.budget.can_start_candidate(
            spec.stage_id,
            atom_count=spec.estimated_atom_count,
            default_atom_seconds=default_atom_seconds,
        ):
            self.state.status = RunStatus.BUDGET_EXHAUSTED
            self.state.add_event(
                "budget-stop",
                stage_id=spec.stage_id,
                config_id=spec.config_id,
                remaining_seconds=self.budget.remaining_seconds,
            )
            self.checkpoint()
            raise BudgetExhaustedError(
                f"remaining budget cannot complete {spec.stage_id}/"
                f"{spec.config_id}"
            )

        task.transition(TaskStatus.RUNNING)
        self.state.status = RunStatus.RUNNING
        self.state.add_event("task-started", task_id=task.task_id)
        self.checkpoint()
        started = self.monotonic()
        context = TaskContext(self.state, self.store, spec, task)
        max_attempts = 1 + len(self.retry_delays)

        while task.attempts < max_attempts:
            task.attempts += 1
            try:
                output = callback(context) or TaskOutput()
                if not isinstance(output, TaskOutput):
                    raise DeterministicTaskError(
                        "task callback must return TaskOutput or None"
                    )
                for artifact in output.artifacts:
                    self.store.verify_artifact(artifact)
                task.artifacts = list(output.artifacts)
                task.metadata.update(dict(output.metadata))
                duration = max(0.0, self.monotonic() - started)
                task.duration_seconds = duration
                task.transition(TaskStatus.COMPLETED)
                self._record_duration(spec.stage_id, duration)
                self.state.add_event(
                    "task-completed",
                    task_id=task.task_id,
                    attempts=task.attempts,
                    duration_seconds=duration,
                )
                self.checkpoint()
                return task
            except TransientTaskError as exc:
                if task.attempts >= max_attempts:
                    task.error = str(exc)
                    task.duration_seconds = max(
                        0.0, self.monotonic() - started
                    )
                    task.transition(TaskStatus.FAILED)
                    self.state.status = RunStatus.PARTIAL
                    self.state.add_event(
                        "task-failed",
                        task_id=task.task_id,
                        error=str(exc),
                        transient=True,
                    )
                    self.checkpoint()
                    raise
                delay = self.retry_delays[task.attempts - 1]
                task.error = str(exc)
                self.state.add_event(
                    "task-retry",
                    task_id=task.task_id,
                    attempt=task.attempts,
                    delay_seconds=delay,
                    error=str(exc),
                )
                self.checkpoint()
                self.sleep(delay)
            except BlockedTaskError as exc:
                task.error = str(exc)
                task.duration_seconds = max(
                    0.0, self.monotonic() - started
                )
                task.transition(TaskStatus.BLOCKED)
                self.state.status = RunStatus.BLOCKED
                self.state.add_event(
                    "task-blocked", task_id=task.task_id, error=str(exc)
                )
                self.checkpoint()
                raise
            except (DeterministicTaskError, ArtifactIntegrityError) as exc:
                task.error = str(exc)
                task.duration_seconds = max(
                    0.0, self.monotonic() - started
                )
                task.transition(TaskStatus.FAILED)
                self.state.status = RunStatus.FAILED
                self.state.add_event(
                    "task-failed",
                    task_id=task.task_id,
                    error=str(exc),
                    transient=False,
                )
                self.checkpoint()
                raise
            except Exception as exc:
                task.error = f"{type(exc).__name__}: {exc}"
                task.duration_seconds = max(
                    0.0, self.monotonic() - started
                )
                task.transition(TaskStatus.FAILED)
                self.state.status = RunStatus.FAILED
                self.state.add_event(
                    "task-failed",
                    task_id=task.task_id,
                    error=task.error,
                    transient=False,
                )
                self.checkpoint()
                raise
        raise AssertionError("bounded retry loop exited unexpectedly")

    def run_specs(
        self,
        specs: Iterable[TaskSpec],
        callback: TaskCallback,
        *,
        default_atom_seconds: float | None = None,
        continue_after_terminal_error: bool = True,
    ) -> tuple[TaskRecord, ...]:
        records: list[TaskRecord] = []
        try:
            for spec in specs:
                try:
                    records.append(
                        self.run_task(
                            spec,
                            callback,
                            default_atom_seconds=default_atom_seconds,
                        )
                    )
                except BudgetExhaustedError:
                    break
                except (BlockedTaskError, DeterministicTaskError, TransientTaskError):
                    if not continue_after_terminal_error:
                        raise
        finally:
            self.checkpoint()
        return tuple(records)

    def write_report(self, callback: ReportCallback) -> tuple[ArtifactRecord, ...]:
        artifacts = tuple(callback(self.state, self.store))
        for artifact in artifacts:
            self.store.verify_artifact(artifact)
        self.state.report_artifacts = list(artifacts)
        self.state.add_event(
            "report-written",
            artifacts=[artifact.path for artifact in artifacts],
        )
        self.checkpoint()
        return artifacts

    def finalize(self) -> RunStatus:
        failed = sum(
            task.status is TaskStatus.FAILED
            for task in self.state.tasks.values()
        )
        blocked = sum(
            task.status is TaskStatus.BLOCKED
            for task in self.state.tasks.values()
        )
        pending = sum(
            task.status is TaskStatus.PENDING
            for task in self.state.tasks.values()
        )
        completed = sum(
            task.status is TaskStatus.COMPLETED
            for task in self.state.tasks.values()
        )
        if self.state.status is RunStatus.BUDGET_EXHAUSTED:
            status = RunStatus.BUDGET_EXHAUSTED
        elif blocked:
            status = RunStatus.BLOCKED
        elif failed:
            status = RunStatus.PARTIAL if completed else RunStatus.FAILED
        elif pending:
            status = RunStatus.PARTIAL
        elif self.state.status is RunStatus.FAILED:
            status = RunStatus.FAILED
        else:
            status = RunStatus.COMPLETED
        self.state.status = status
        self.state.add_event("run-finalized", status=status.value)
        self.checkpoint()
        return status


def candidate_is_complete(
    state: RunState,
    *,
    stage_id: str,
    config_id: str,
    required_paper_ids: Iterable[str],
    input_fingerprint: str,
) -> bool:
    """Return true only when the candidate completed the full paper set."""

    required = set(required_paper_ids)
    completed = {
        task.paper_id
        for task in state.tasks.values()
        if task.stage_id == stage_id
        and task.config_id == config_id
        and task.input_fingerprint == input_fingerprint
        and task.status is TaskStatus.COMPLETED
    }
    return completed == required


def build_report_payload(
    state: RunState,
    *,
    mapping_coverage: Mapping[str, Any] | None = None,
    candidates: Sequence[Mapping[str, Any]] = (),
    bootstrap: Mapping[str, Any] | None = None,
    pareto_frontier: Sequence[Mapping[str, Any]] = (),
    provisional_winner: str | None = None,
    hardware_fingerprints: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build the stable machine-readable report interface used by adapters."""

    counts = {
        status.value: sum(
            task.status is status for task in state.tasks.values()
        )
        for status in TaskStatus
    }
    return {
        "schema_version": 1,
        "run_id": state.run_id,
        "status": state.status.value,
        "created_at": state.created_at,
        "updated_at": state.updated_at,
        "budget_seconds": state.budget_seconds,
        "elapsed_seconds": state.elapsed_seconds,
        "fingerprints": dict(sorted(state.fingerprints.items())),
        "hardware_fingerprints": dict(
            sorted((hardware_fingerprints or {}).items())
        ),
        "task_counts": counts,
        "mapping_coverage": (
            dict(mapping_coverage) if mapping_coverage is not None else None
        ),
        "candidates": [dict(candidate) for candidate in candidates],
        "bootstrap": dict(bootstrap) if bootstrap is not None else None,
        "pareto_frontier": [dict(item) for item in pareto_frontier],
        "provisional_winner": provisional_winner,
        "completed": [
            task.task_id
            for task in state.tasks.values()
            if task.status is TaskStatus.COMPLETED
        ],
        "partial": [
            task.task_id
            for task in state.tasks.values()
            if task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}
        ],
        "blocked": [
            task.to_dict()
            for task in state.tasks.values()
            if task.status is TaskStatus.BLOCKED
        ],
        "failed": [
            task.to_dict()
            for task in state.tasks.values()
            if task.status is TaskStatus.FAILED
        ],
    }
