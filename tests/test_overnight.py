from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, FormatChecker

from benchmarks.overnight import (
    ArtifactIntegrityError,
    BudgetExhaustedError,
    FingerprintMismatchError,
    OvernightRunner,
    RunStatus,
    RunStore,
    StateContractError,
    TaskOutput,
    TaskSpec,
    TaskStatus,
    TransientTaskError,
    atomic_task_id,
    build_report_payload,
    candidate_is_complete,
)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


def _store(tmp_path: Path, *, budget_seconds: float = 100.0):
    store = RunStore(tmp_path / "run")
    state = store.create(
        run_id="rq2-test",
        fingerprints={"code": "code-v1", "config": "config-v1"},
        budget_seconds=budget_seconds,
    )
    return store, state


def _spec(
    *,
    stage: str = "retriever",
    paper: str = "p1",
    config: str = "dense",
    fingerprint: str = "input-v1",
) -> TaskSpec:
    return TaskSpec(
        stage_id=stage,
        paper_id=paper,
        config_id=config,
        input_fingerprint=fingerprint,
    )


def test_atomic_task_id_uses_every_identity_component():
    base = atomic_task_id(
        run_id="run",
        stage_id="retriever",
        paper_id="p1",
        config_id="dense",
        input_fingerprint="inputs",
    )
    changed = atomic_task_id(
        run_id="run",
        stage_id="retriever",
        paper_id="p1",
        config_id="bm25",
        input_fingerprint="inputs",
    )

    assert base != changed
    assert base.startswith("retriever-")


def test_task_checkpoint_is_atomic_and_hash_verified(tmp_path):
    store, state = _store(tmp_path)
    clock = FakeClock()
    runner = OvernightRunner(
        state=state,
        store=store,
        retry_delays=(),
        monotonic=clock,
    )

    def callback(context):
        clock.advance(2.5)
        artifact = context.store.write_json_artifact(
            "raw-results/p1-dense.json",
            {"hits": ["c1"]},
            schema_id="researchqa-raw-result-v1",
        )
        return TaskOutput((artifact,), {"query_count": 1})

    task = runner.run_task(_spec(), callback)

    assert task.status is TaskStatus.COMPLETED
    assert task.duration_seconds == 2.5
    assert task.metadata["query_count"] == 1
    reloaded = store.load(
        expected_fingerprints={"code": "code-v1", "config": "config-v1"}
    )
    assert reloaded.tasks[task.task_id].status is TaskStatus.COMPLETED
    assert not list(store.root.rglob("*.tmp"))

    artifact_path = store.root / task.artifacts[0].path
    artifact_path.write_text('{"tampered":true}\n', encoding="utf-8")
    with pytest.raises(ArtifactIntegrityError, match="hash mismatch"):
        store.load()


def test_resume_rejects_changed_run_fingerprint(tmp_path):
    store, _ = _store(tmp_path)

    with pytest.raises(FingerprintMismatchError, match="code"):
        store.load(expected_fingerprints={"code": "code-v2"})


def test_running_task_is_reset_to_pending_after_interruption(tmp_path):
    store, state = _store(tmp_path)
    task = state.ensure_task(_spec())
    task.transition(TaskStatus.RUNNING)
    store.save(state)

    resumed = store.load(recover_running=True)
    recovered = resumed.tasks[task.task_id]

    assert recovered.status is TaskStatus.PENDING
    assert recovered.interruptions == 1
    assert resumed.status is RunStatus.INTERRUPTED


def test_transient_failures_retry_with_fixed_backoff(tmp_path):
    store, state = _store(tmp_path)
    sleeps: list[float] = []
    runner = OvernightRunner(
        state=state,
        store=store,
        retry_delays=(5, 20, 60),
        sleep=sleeps.append,
    )
    attempts = 0

    def callback(_context):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise TransientTaskError("temporary Ollama failure")
        return TaskOutput()

    task = runner.run_task(_spec(), callback)

    assert task.status is TaskStatus.COMPLETED
    assert task.attempts == 3
    assert sleeps == [5.0, 20.0]


def test_deterministic_terminal_task_cannot_be_restarted(tmp_path):
    store, state = _store(tmp_path)
    runner = OvernightRunner(
        state=state,
        store=store,
        retry_delays=(),
    )

    with pytest.raises(ValueError):
        # The callback's error is not retryable and the task becomes failed.
        runner.run_task(_spec(), lambda _context: (_ for _ in ()).throw(ValueError("bad")))

    task = next(iter(state.tasks.values()))
    assert task.status is TaskStatus.FAILED
    with pytest.raises(StateContractError, match="terminal task"):
        runner.run_task(_spec(), lambda _context: TaskOutput())


def test_budget_stops_before_candidate_that_cannot_finish(tmp_path):
    store, state = _store(tmp_path, budget_seconds=10)
    clock = FakeClock()
    runner = OvernightRunner(
        state=state,
        store=store,
        retry_delays=(),
        monotonic=clock,
    )
    # Establish a five-second moving average for the stage.
    first = _spec(paper="p1")

    def callback(_context):
        clock.advance(5)
        return TaskOutput()

    runner.run_task(first, callback)
    clock.advance(1)
    second = TaskSpec(
        stage_id="retriever",
        paper_id="p2",
        config_id="dense",
        input_fingerprint="input-v1",
        estimated_atom_count=2,
    )

    with pytest.raises(BudgetExhaustedError):
        runner.run_task(second, callback)

    assert state.status is RunStatus.BUDGET_EXHAUSTED
    assert state.tasks[second.task_id(state.run_id)].status is TaskStatus.PENDING


def test_candidate_is_rankable_only_for_the_complete_paper_set(tmp_path):
    store, state = _store(tmp_path)
    runner = OvernightRunner(state=state, store=store, retry_delays=())
    for paper_id in ("p1", "p2"):
        runner.run_task(
            _spec(paper=paper_id),
            lambda _context: TaskOutput(),
        )

    assert candidate_is_complete(
        state,
        stage_id="retriever",
        config_id="dense",
        required_paper_ids=["p1", "p2"],
        input_fingerprint="input-v1",
    )
    assert not candidate_is_complete(
        state,
        stage_id="retriever",
        config_id="dense",
        required_paper_ids=["p1", "p2", "p3"],
        input_fingerprint="input-v1",
    )


def test_report_interface_keeps_partial_failed_and_blocked_explicit(tmp_path):
    store, state = _store(tmp_path)
    pending = state.ensure_task(_spec(paper="p1"))
    failed = state.ensure_task(_spec(paper="p2"))
    failed.transition(TaskStatus.RUNNING)
    failed.error = "schema mismatch"
    failed.transition(TaskStatus.FAILED)
    state.status = RunStatus.PARTIAL

    report = build_report_payload(
        state,
        mapping_coverage={"overall": 0.97},
        provisional_winner=None,
    )

    assert pending.task_id in report["partial"]
    assert report["failed"][0]["task_id"] == failed.task_id
    assert report["mapping_coverage"]["overall"] == 0.97
    assert report["provisional_winner"] is None

    artifact = store.write_json_artifact("report/run-manifest.json", report)
    payload = json.loads((store.root / artifact.path).read_text(encoding="utf-8"))
    assert payload["status"] == "partial"


def test_candidate_and_rq2_overnight_configs_share_schema_without_drift():
    benchmark_root = Path(__file__).resolve().parents[1] / "benchmarks"
    schema = json.loads(
        (benchmark_root / "schemas" / "config.schema.json").read_text(
            encoding="utf-8"
        )
    )
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    loaded = {}
    for filename in ("baseline-fixed-800.yaml", "rq2-overnight.yaml"):
        value = yaml.safe_load(
            (benchmark_root / "configs" / filename).read_text(encoding="utf-8")
        )
        assert not list(validator.iter_errors(value)), filename
        loaded[filename] = value

    invalid = deepcopy(loaded["rq2-overnight.yaml"])
    invalid["benchmark"]["paper_count"] = 19
    assert list(validator.iter_errors(invalid))


def test_run_state_and_minimum_report_match_their_json_schemas(tmp_path):
    benchmark_root = Path(__file__).resolve().parents[1] / "benchmarks"
    store, state = _store(tmp_path)
    report = build_report_payload(state)

    for filename, payload in (
        ("researchqa-run-state.schema.json", state.to_dict()),
        ("researchqa-report.schema.json", report),
    ):
        schema = json.loads(
            (benchmark_root / "schemas" / filename).read_text(encoding="utf-8")
        )
        errors = list(
            Draft202012Validator(
                schema, format_checker=FormatChecker()
            ).iter_errors(payload)
        )
        assert not errors, [error.message for error in errors]


def test_finalize_keeps_a_pre_task_runner_failure_explicit(tmp_path):
    store, state = _store(tmp_path)
    state.status = RunStatus.FAILED
    runner = OvernightRunner(state=state, store=store, retry_delays=())

    assert runner.finalize() is RunStatus.FAILED
