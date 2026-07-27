#!/usr/bin/env python3
"""Run or inspect the recoverable ResearchQA rq-2 overnight sweep.

The CLI owns configuration validation, immutable run fingerprints, state
resume, budget enforcement, and minimum reports.  Live source, note, chunking,
retrieval, and reranking work is supplied through a late-imported adapter so
this entry point does not depend on unfinished implementation signatures.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml
from jsonschema import Draft202012Validator, FormatChecker


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from benchmarks.overnight import (  # noqa: E402
    ArtifactRecord,
    OvernightAdapter,
    OvernightError,
    OvernightRunner,
    RunState,
    RunStatus,
    RunStore,
    StateContractError,
    TaskSpec,
    build_report_payload,
    canonical_json_bytes,
    fingerprint_payload,
    sha256_path,
)


BENCHMARK_ROOT = REPOSITORY_ROOT / "benchmarks"
DEFAULT_CONFIG = BENCHMARK_ROOT / "configs" / "rq2-overnight.yaml"
DEFAULT_SCHEMA = BENCHMARK_ROOT / "schemas" / "config.schema.json"
SAFE_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
EXECUTION_COMMANDS = frozenset({"prepare", "canary", "run"})


class CliContractError(ValueError):
    """Raised when CLI inputs or a late-loaded adapter violate the contract."""


def load_config(
    config_path: Path = DEFAULT_CONFIG,
    schema_path: Path = DEFAULT_SCHEMA,
) -> dict[str, Any]:
    """Load and validate the committed rq-2 overnight configuration."""

    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise CliContractError("overnight config must be a YAML mapping")
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors = sorted(
        validator.iter_errors(config),
        key=lambda error: tuple(str(part) for part in error.absolute_path),
    )
    if errors:
        rendered = "; ".join(
            f"{_json_path(error.absolute_path)}: {error.message}"
            for error in errors
        )
        raise CliContractError(f"invalid overnight config: {rendered}")
    if config.get("config_kind") != "researchqa-overnight":
        raise CliContractError(
            "config must select the researchqa-overnight schema branch"
        )
    return config


def _json_path(parts: Sequence[object]) -> str:
    rendered = "$"
    for part in parts:
        rendered += f"[{part}]" if isinstance(part, int) else f".{part}"
    return rendered


def resolve_run_root(
    config: Mapping[str, Any],
    *,
    run_id: str | None,
    run_root: Path | None,
) -> tuple[str, Path]:
    """Resolve a safe run ID and directory without creating either."""

    if run_root is None and run_id is None:
        raise CliContractError("--run-id is required unless --run-root is given")
    resolved_root: Path
    if run_root is None:
        assert run_id is not None
        resolved_root = (
            Path(str(config["paths"]["cache_root"]))
            / str(config["paths"]["runs_dir"])
            / run_id
        ).resolve(strict=False)
    else:
        resolved_root = run_root.resolve(strict=False)
        inferred = resolved_root.name
        if run_id is not None and run_id != inferred:
            raise CliContractError(
                f"--run-id {run_id!r} does not match --run-root name "
                f"{inferred!r}"
            )
        run_id = run_id or inferred
    assert run_id is not None
    if not SAFE_RUN_ID.fullmatch(run_id):
        raise CliContractError(
            "run ID must match ^[a-z0-9][a-z0-9._-]{0,127}$"
        )
    return run_id, resolved_root


def _code_fingerprint() -> str:
    """Fingerprint committed runner inputs, including dirty file contents."""

    roots = (
        BENCHMARK_ROOT,
        REPOSITORY_ROOT / "scanner",
        REPOSITORY_ROOT / "scripts",
    )
    records: list[tuple[str, int, str]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            if (
                not path.is_file()
                or "__pycache__" in path.parts
                or ".cache" in path.parts
                or path.suffix.lower()
                not in {".py", ".json", ".yaml", ".yml", ".ps1"}
            ):
                continue
            size, digest = sha256_path(path)
            records.append(
                (path.relative_to(REPOSITORY_ROOT).as_posix(), size, digest)
            )
    return fingerprint_payload(records)


def base_fingerprints(config: Mapping[str, Any]) -> dict[str, str]:
    """Return fingerprints available before the live adapter runs."""

    models = config["models"]
    return {
        "code": _code_fingerprint(),
        "config": fingerprint_payload(config),
        "embedding-model": str(models["embedding"]["digest"]),
        "reranker-model": str(models["reranker"]["revision"]),
    }


def _load_adapter(
    adapter_spec: str,
    *,
    config: Mapping[str, Any],
    run_root: Path,
) -> OvernightAdapter:
    """Late-load ``module:factory`` and instantiate the stable adapter boundary."""

    module_name, separator, attribute_name = adapter_spec.partition(":")
    if not separator or not module_name or not attribute_name:
        raise CliContractError("--adapter must use module:factory syntax")
    module = importlib.import_module(module_name)
    try:
        factory = getattr(module, attribute_name)
    except AttributeError as exc:
        raise CliContractError(
            f"adapter attribute does not exist: {adapter_spec}"
        ) from exc
    if not callable(factory):
        raise CliContractError(f"adapter factory is not callable: {adapter_spec}")
    adapter = factory(config=config, run_root=run_root)
    required = ("task_specs", "run_task")
    missing = [name for name in required if not callable(getattr(adapter, name, None))]
    if missing:
        raise CliContractError(
            f"adapter {adapter_spec} is missing callbacks: {', '.join(missing)}"
        )
    return adapter


def _adapter_fingerprints(
    adapter: OvernightAdapter,
    *,
    command: str,
) -> dict[str, str]:
    callback = getattr(adapter, "fingerprints", None)
    if callback is None:
        return {}
    values = callback(command=command)
    if not isinstance(values, Mapping) or not all(
        isinstance(key, str)
        and key
        and isinstance(value, str)
        and value
        for key, value in values.items()
    ):
        raise CliContractError(
            "adapter fingerprints(command=...) must return a string mapping"
        )
    return dict(values)


def _default_report(
    state: RunState,
    store: RunStore,
    *,
    existing_paths: frozenset[str] = frozenset(),
) -> tuple[ArtifactRecord, ...]:
    manifest = build_report_payload(state)
    counts = manifest["task_counts"]
    markdown = "\n".join(
        (
            "# ResearchQA rq-2 overnight report",
            "",
            f"- Run ID: `{state.run_id}`",
            f"- Status: `{state.status.value}`",
            f"- Elapsed: {state.elapsed_seconds:.3f} / "
            f"{state.budget_seconds:.3f} seconds",
            f"- Tasks: {counts['completed']} completed, "
            f"{counts['pending']} pending, {counts['blocked']} blocked, "
            f"{counts['failed']} failed",
            f"- Provisional winner: `{manifest['provisional_winner']}`",
            "",
            "This is the minimum fail-closed runner report. A live adapter may "
            "add leaderboard, breakdown, bootstrap, Pareto, and mapping artifacts.",
            "",
            "The runner stops here and does not start rq-5 automatically.",
            "",
        )
    )
    artifacts: list[ArtifactRecord] = []
    if "report/run-manifest.json" not in existing_paths:
        artifacts.append(
            store.write_json_artifact(
                "report/run-manifest.json",
                manifest,
                schema_id="researchqa-report-v1",
            )
        )
    if "report/morning-report.md" not in existing_paths:
        artifacts.append(
            store.write_bytes_artifact(
                "report/morning-report.md",
                markdown.encode("utf-8"),
                media_type="text/markdown",
                schema_id="researchqa-morning-report-v1",
            )
        )
    return tuple(artifacts)


def _write_reports(
    runner: OvernightRunner,
    adapter: OvernightAdapter | None,
) -> tuple[ArtifactRecord, ...]:
    """Always emit minimum reports, retaining any extra adapter artifacts."""

    def callback(
        state: RunState,
        store: RunStore,
    ) -> tuple[ArtifactRecord, ...]:
        adapter_artifacts: tuple[ArtifactRecord, ...] = ()
        report_callback = getattr(adapter, "write_report", None)
        if callable(report_callback):
            try:
                adapter_artifacts = tuple(report_callback(state, store))
                for artifact in adapter_artifacts:
                    store.verify_artifact(artifact)
            except Exception as exc:
                state.add_event(
                    "adapter-report-failed",
                    error=f"{type(exc).__name__}: {exc}",
                )
                adapter_artifacts = ()
        paths = {artifact.path for artifact in adapter_artifacts}
        minimum = _default_report(
            state,
            store,
            existing_paths=frozenset(paths),
        )
        return adapter_artifacts + minimum

    return runner.write_report(callback)


def _state_summary(state: RunState, store: RunStore) -> dict[str, Any]:
    counts = {
        status: sum(task.status.value == status for task in state.tasks.values())
        for status in ("pending", "running", "completed", "failed", "blocked")
    }
    return {
        "run_id": state.run_id,
        "run_root": str(store.root),
        "status": state.status.value,
        "elapsed_seconds": state.elapsed_seconds,
        "budget_seconds": state.budget_seconds,
        "remaining_seconds": max(
            0.0, state.budget_seconds - state.elapsed_seconds
        ),
        "task_counts": counts,
        "updated_at": state.updated_at,
        "report_artifacts": [
            artifact.to_dict() for artifact in state.report_artifacts
        ],
    }


def _load_or_create_state(
    *,
    store: RunStore,
    run_id: str,
    fingerprints: Mapping[str, str],
    budget_seconds: float,
) -> RunState:
    if store.state_path.exists():
        return store.load(expected_fingerprints=fingerprints)
    return store.create(
        run_id=run_id,
        fingerprints=fingerprints,
        budget_seconds=budget_seconds,
    )


def _execute(
    *,
    command: str,
    config: Mapping[str, Any],
    run_id: str,
    run_root: Path,
    adapter_spec: str,
    default_atom_seconds: float | None,
) -> int:
    adapter = _load_adapter(adapter_spec, config=config, run_root=run_root)
    fingerprints = base_fingerprints(config)
    fingerprints.update(_adapter_fingerprints(adapter, command=command))
    store = RunStore(run_root)
    state = _load_or_create_state(
        store=store,
        run_id=run_id,
        fingerprints=fingerprints,
        budget_seconds=float(config["budget"]["wall_clock_seconds"]),
    )
    runner = OvernightRunner(
        state=state,
        store=store,
        retry_delays=config["budget"]["retry_delays_seconds"],
    )

    error: Exception | None = None
    try:
        specs = tuple(adapter.task_specs(command, state))
        if not specs:
            raise CliContractError(
                f"adapter returned no task specs for {command!r}; "
                "refusing to report a successful empty run"
            )
        if not all(isinstance(spec, TaskSpec) for spec in specs):
            raise CliContractError(
                "adapter task_specs must yield benchmarks.overnight.TaskSpec"
            )
        runner.run_specs(
            specs,
            adapter.run_task,
            default_atom_seconds=default_atom_seconds,
        )
    except Exception as exc:
        error = exc
        if not isinstance(exc, OvernightError):
            state.status = RunStatus.FAILED
            state.add_event(
                "runner-failed",
                command=command,
                error=f"{type(exc).__name__}: {exc}",
            )
            runner.checkpoint()
    finally:
        status = runner.finalize()
        _write_reports(runner, adapter)

    print(json.dumps(_state_summary(state, store), indent=2, sort_keys=True))
    if error is not None:
        print(f"[FAIL] {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0 if status is RunStatus.COMPLETED else 2


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the recoverable ResearchQA rq-2 overnight sweep."
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    parser.add_argument("--run-id")
    parser.add_argument("--run-root", type=Path)
    parser.add_argument(
        "--adapter",
        default=os.environ.get("RESEARCHQA_OVERNIGHT_ADAPTER"),
        help=(
            "Late-bound module:factory. The factory receives config= and "
            "run_root= and returns task_specs/run_task callbacks."
        ),
    )
    parser.add_argument(
        "--default-atom-seconds",
        type=float,
        help="First-candidate estimate before a stage moving average exists.",
    )
    parser.add_argument(
        "command",
        choices=("prepare", "canary", "run", "report", "status"),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        config = load_config(args.config, args.schema)
        run_id, run_root = resolve_run_root(
            config,
            run_id=args.run_id,
            run_root=args.run_root,
        )
        store = RunStore(run_root)

        if args.command == "status":
            state = store.load()
            print(json.dumps(_state_summary(state, store), indent=2, sort_keys=True))
            return 0

        if args.command == "report":
            state = store.load()
            runner = OvernightRunner(
                state=state,
                store=store,
                retry_delays=config["budget"]["retry_delays_seconds"],
            )
            adapter = (
                _load_adapter(args.adapter, config=config, run_root=run_root)
                if args.adapter
                else None
            )
            artifacts = _write_reports(runner, adapter)
            print(
                json.dumps(
                    {
                        **_state_summary(state, store),
                        "written": [artifact.path for artifact in artifacts],
                    },
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if not args.adapter:
            raise CliContractError(
                f"{args.command} requires --adapter module:factory or "
                "RESEARCHQA_OVERNIGHT_ADAPTER"
            )
        return _execute(
            command=args.command,
            config=config,
            run_id=run_id,
            run_root=run_root,
            adapter_spec=args.adapter,
            default_atom_seconds=args.default_atom_seconds,
        )
    except (
        CliContractError,
        ImportError,
        OSError,
        OvernightError,
        json.JSONDecodeError,
        yaml.YAMLError,
    ) as exc:
        print(f"[FAIL] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
