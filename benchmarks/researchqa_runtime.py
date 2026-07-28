"""Fail-closed live runtime assembly for the ResearchQA rq-2 sweep."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from benchmarks.researchqa_models import (
    OLLAMA_EMBED_DIMENSIONS,
    OLLAMA_EMBED_MODEL_DIGEST,
    OLLAMA_EMBED_MODEL_ID,
    OllamaBatchEmbeddingClient,
    Qwen3RerankerTransformersAdapter,
)
from benchmarks.researchqa_notes import GENERIC_TEMPLATE
from benchmarks.researchqa_retrieval import (
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
)
from benchmarks.researchqa_strategy import (
    load_main_documents,
    normalize_paper_id,
)
from benchmarks.researchqa_sweep import (
    StrategySweepResult,
    run_strategy_sweep,
)


RUNTIME_SCHEMA_VERSION = 1
EXPECTED_PAPER_COUNT = 20
EXPECTED_QUESTION_COUNT = 254
_PAPER_ID_RE = re.compile(r"^W\d+$")

EmbeddingFactory = Callable[..., object]
RerankerFactory = Callable[..., object]
SweepRunner = Callable[..., StrategySweepResult]


class ResearchQARuntimeError(RuntimeError):
    """Raised when the live runtime cannot preserve its input/lifecycle contract."""


@dataclass(frozen=True)
class ResearchQARuntimeResult:
    """Completed sweep plus the two runtime-owned audit artifacts."""

    sweep_result: StrategySweepResult
    model_preflight_path: str
    runtime_summary_path: str


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temp_path = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)
    return path


def _read_jsonl(path: Path, *, label: str) -> list[Mapping[str, Any]]:
    if not path.is_file():
        raise ResearchQARuntimeError(f"{label} does not exist: {path}")
    rows: list[Mapping[str, Any]] = []
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        1,
    ):
        if not raw_line.strip():
            raise ResearchQARuntimeError(
                f"{label} contains a blank line at {line_number}: {path}"
            )
        try:
            row = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ResearchQARuntimeError(
                f"{label} line {line_number} is invalid JSON: {exc}"
            ) from exc
        if not isinstance(row, Mapping):
            raise ResearchQARuntimeError(
                f"{label} line {line_number} must be a JSON object"
            )
        rows.append(row)
    return rows


def _strict_paper_id(value: object, *, label: str) -> str:
    try:
        paper_id = normalize_paper_id(value)
    except Exception as exc:
        raise ResearchQARuntimeError(f"{label} has an invalid paper_id") from exc
    if not _PAPER_ID_RE.fullmatch(paper_id):
        raise ResearchQARuntimeError(
            f"{label} paper_id must be a normalized OpenAlex work ID: {paper_id!r}"
        )
    return paper_id


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def load_suite_questions(
    config: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], tuple[str, ...], Path]:
    """Load the immutable cache-owned rq-2 question set."""

    benchmark = config.get("benchmark")
    paths = config.get("paths")
    if not isinstance(benchmark, Mapping) or not isinstance(paths, Mapping):
        raise ResearchQARuntimeError("config benchmark/paths must be mappings")
    if (
        benchmark.get("tier_id") != "rq-2"
        or benchmark.get("paper_count") != EXPECTED_PAPER_COUNT
        or benchmark.get("question_count") != EXPECTED_QUESTION_COUNT
    ):
        raise ResearchQARuntimeError(
            "runtime requires rq-2 with exactly 20 papers and 254 questions"
        )
    cache_root = paths.get("cache_root")
    suite_dir = paths.get("suite_dir")
    if not isinstance(cache_root, str) or not cache_root:
        raise ResearchQARuntimeError("config paths.cache_root must be non-empty")
    if not isinstance(suite_dir, str) or not suite_dir:
        raise ResearchQARuntimeError("config paths.suite_dir must be non-empty")
    question_path = (
        Path(cache_root).resolve(strict=False) / suite_dir / "questions.jsonl"
    ).resolve(strict=False)
    questions = _read_jsonl(question_path, label="suite questions")
    if len(questions) != EXPECTED_QUESTION_COUNT:
        raise ResearchQARuntimeError(
            f"suite questions must contain exactly {EXPECTED_QUESTION_COUNT} "
            f"rows, found {len(questions)}"
        )

    row_ids: set[str] = set()
    paper_ids: set[str] = set()
    for index, question in enumerate(questions, 1):
        row_id = question.get("row_id")
        if not isinstance(row_id, str) or not row_id.strip():
            raise ResearchQARuntimeError(
                f"suite question {index} has an empty row_id"
            )
        if row_id in row_ids:
            raise ResearchQARuntimeError(
                f"suite questions contain duplicate row_id {row_id!r}"
            )
        row_ids.add(row_id)
        paper_ids.add(
            _strict_paper_id(
                question.get("paper_id"),
                label=f"suite question {row_id!r}",
            )
        )
    if len(paper_ids) != EXPECTED_PAPER_COUNT:
        raise ResearchQARuntimeError(
            f"suite questions must cover exactly {EXPECTED_PAPER_COUNT} papers, "
            f"found {len(paper_ids)}"
        )
    return questions, tuple(sorted(paper_ids)), question_path


def load_frozen_notes(
    run_root: str | Path,
    *,
    expected_paper_ids: Sequence[str],
) -> tuple[dict[str, str], Path]:
    """Load and SHA-bind the exact frozen 20-note runtime input."""

    root = Path(run_root).resolve(strict=False)
    frozen_root = root / "note-runs" / "frozen"
    manifest_path = frozen_root / "frozen-notes.jsonl"
    rows = _read_jsonl(manifest_path, label="frozen note manifest")
    if len(rows) != EXPECTED_PAPER_COUNT:
        raise ResearchQARuntimeError(
            f"frozen note manifest must contain exactly {EXPECTED_PAPER_COUNT} "
            f"rows, found {len(rows)}"
        )

    expected = {
        _strict_paper_id(value, label="expected paper set")
        for value in expected_paper_ids
    }
    if len(expected) != EXPECTED_PAPER_COUNT:
        raise ResearchQARuntimeError(
            f"expected paper set must contain exactly {EXPECTED_PAPER_COUNT} "
            f"papers, found {len(expected)}"
        )

    notes: dict[str, str] = {}
    for index, row in enumerate(rows, 1):
        paper_id = _strict_paper_id(
            row.get("paper_id"),
            label=f"frozen note row {index}",
        )
        if row.get("paper_id") != paper_id:
            raise ResearchQARuntimeError(
                f"frozen note row {index} paper_id must already be normalized"
            )
        if paper_id in notes:
            raise ResearchQARuntimeError(
                f"frozen note manifest contains duplicate paper_id {paper_id}"
            )
        if row.get("schema_version") != 1:
            raise ResearchQARuntimeError(
                f"frozen note {paper_id} has unsupported schema_version"
            )
        if row.get("template") != GENERIC_TEMPLATE:
            raise ResearchQARuntimeError(
                f"frozen note {paper_id} must use template {GENERIC_TEMPLATE}"
            )
        expected_sha = row.get("note_sha256")
        if not _is_sha256(expected_sha):
            raise ResearchQARuntimeError(
                f"frozen note {paper_id} has an invalid note_sha256"
            )

        note_path = frozen_root / "notes" / f"{paper_id}.md"
        if not note_path.is_file():
            raise ResearchQARuntimeError(
                f"frozen note file does not exist for {paper_id}: {note_path}"
            )
        note_bytes = note_path.read_bytes()
        actual_sha = _sha256_bytes(note_bytes)
        if actual_sha != expected_sha:
            raise ResearchQARuntimeError(
                f"frozen note SHA-256 mismatch for {paper_id}: "
                f"expected {expected_sha}, found {actual_sha}"
            )
        try:
            note = note_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ResearchQARuntimeError(
                f"frozen note {paper_id} is not valid UTF-8"
            ) from exc
        if not note.strip():
            raise ResearchQARuntimeError(f"frozen note {paper_id} is empty")
        notes[paper_id] = note

    if set(notes) != expected:
        raise ResearchQARuntimeError(
            "frozen note paper set differs from suite questions: "
            f"missing={sorted(expected - set(notes))}, "
            f"unexpected={sorted(set(notes) - expected)}"
        )
    return dict(sorted(notes.items())), manifest_path


def _validate_model_config(config: Mapping[str, Any]) -> None:
    retrieval = config.get("retrieval")
    if (
        not isinstance(retrieval, Mapping)
        or retrieval.get("scope") != "paper-scoped"
    ):
        raise ResearchQARuntimeError(
            "config retrieval.scope must be exactly paper-scoped"
        )
    models = config.get("models")
    if not isinstance(models, Mapping):
        raise ResearchQARuntimeError("config models must be a mapping")
    embedding = models.get("embedding")
    reranker = models.get("reranker")
    if not isinstance(embedding, Mapping) or not isinstance(reranker, Mapping):
        raise ResearchQARuntimeError(
            "config models.embedding/models.reranker must be mappings"
        )
    expected_embedding = {
        "model": OLLAMA_EMBED_MODEL_ID,
        "digest": OLLAMA_EMBED_MODEL_DIGEST,
        "dimensions": OLLAMA_EMBED_DIMENSIONS,
    }
    mismatched_embedding = {
        key: embedding.get(key)
        for key, expected in expected_embedding.items()
        if embedding.get(key) != expected
    }
    if mismatched_embedding:
        raise ResearchQARuntimeError(
            f"embedding model config is not pinned: {mismatched_embedding}"
        )
    if (
        reranker.get("model") != RERANKER_MODEL_ID
        or reranker.get("revision") != RERANKER_REVISION
    ):
        raise ResearchQARuntimeError("reranker model config is not pinned")


def _preflight_payload(value: object) -> Mapping[str, Any]:
    to_dict = getattr(value, "to_dict", None)
    payload = to_dict() if callable(to_dict) else value
    if not isinstance(payload, Mapping):
        raise ResearchQARuntimeError(
            "model preflight must return a mapping or expose to_dict()"
        )
    return dict(payload)


def _sweep_summary(result: StrategySweepResult) -> dict[str, Any]:
    return {
        "candidate_count": len(result.records),
        "stage_ids": sorted(result.stage_rankings),
        "provisional_winner": result.provisional_winner,
        "leaderboard_count": len(result.leaderboard),
        "pareto_frontier_count": len(result.pareto_frontier),
        "artifact_paths": list(result.artifact_paths),
    }


def run_researchqa_runtime(
    config: Mapping[str, Any],
    run_root: str | Path,
    *,
    embedding_factory: EmbeddingFactory = OllamaBatchEmbeddingClient,
    reranker_factory: RerankerFactory = Qwen3RerankerTransformersAdapter,
    sweep_runner: SweepRunner = run_strategy_sweep,
) -> ResearchQARuntimeResult:
    """Validate live inputs, enforce model lifecycle, and run the full sweep."""

    root = Path(run_root).resolve(strict=False)
    runtime_root = root / "runtime"
    preflight_path = runtime_root / "model-preflight.json"
    summary_path = runtime_root / "runtime-summary.json"
    embedding_cache_dir = root / "model-cache" / "embeddings"
    hf_cache_dir = root / "model-cache" / "hf-cache"

    embedding: object | None = None
    reranker: object | None = None
    embedding_preflight: Mapping[str, Any] | None = None
    reranker_preflight: Mapping[str, Any] | None = None
    embedding_released = False
    cache_only_entered = False
    reranker_preflighted = False
    sweep_result: StrategySweepResult | None = None
    input_summary: dict[str, Any] = {}

    def write_preflight(*, status: str, error: BaseException | None = None) -> None:
        _atomic_write_json(
            preflight_path,
            {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "status": status,
                "embedding": embedding_preflight,
                "reranker": reranker_preflight,
                "lifecycle": {
                    "embedding_released": embedding_released,
                    "embedding_cache_only": cache_only_entered,
                    "reranker_preflighted": reranker_preflighted,
                },
                "error": (
                    {
                        "type": type(error).__name__,
                        "message": str(error),
                    }
                    if error is not None
                    else None
                ),
            },
        )

    def before_rerank_stage() -> None:
        nonlocal embedding_released
        nonlocal cache_only_entered
        nonlocal reranker_preflight
        nonlocal reranker_preflighted
        if embedding is None or reranker is None:
            raise ResearchQARuntimeError("runtime models are not initialized")
        if cache_only_entered or reranker_preflighted:
            raise ResearchQARuntimeError(
                "before_rerank_stage may only run once"
            )
        embedding.release_model()
        embedding_released = True
        embedding.enter_cache_only()
        if getattr(embedding, "_cache_only", False) is not True:
            raise ResearchQARuntimeError(
                "embedding client did not enter cache-only mode"
            )
        cache_only_entered = True
        reranker_preflight = _preflight_payload(reranker.preflight())
        reranker_preflighted = True
        write_preflight(status="completed")

    def assert_embedding_cache_only(_candidate: object) -> None:
        if (
            not cache_only_entered
            or embedding is None
            or getattr(embedding, "_cache_only", False) is not True
        ):
            raise ResearchQARuntimeError(
                "reranking requires the embedding client to be cache-only"
            )

    failure: BaseException | None = None
    try:
        _validate_model_config(config)
        questions, paper_ids, question_path = load_suite_questions(config)
        frozen_notes, manifest_path = load_frozen_notes(
            root,
            expected_paper_ids=paper_ids,
        )
        documents = load_main_documents(
            root,
            expected_paper_ids=paper_ids,
        )
        if len(documents) != EXPECTED_PAPER_COUNT:
            raise ResearchQARuntimeError(
                f"runtime requires exactly {EXPECTED_PAPER_COUNT} Main documents"
            )
        input_summary = {
            "questions_path": str(question_path),
            "frozen_notes_manifest_path": str(manifest_path),
            "retrieval_scope": config["retrieval"]["scope"],
            "paper_count": len(documents),
            "question_count": len(questions),
            "paper_ids": list(paper_ids),
        }

        embedding = embedding_factory(cache_dir=embedding_cache_dir)
        reranker = reranker_factory(hf_home=hf_cache_dir, device="cuda")
        embedding_preflight = _preflight_payload(embedding.preflight())
        write_preflight(status="embedding-preflighted")

        sweep_result = sweep_runner(
            config=config,
            run_root=root,
            documents=documents,
            questions=questions,
            frozen_notes=frozen_notes,
            embedder=embedding,
            reranker=reranker,
            before_rerank_stage=before_rerank_stage,
            assert_embedding_cache_only=assert_embedding_cache_only,
        )
        if not isinstance(sweep_result, StrategySweepResult):
            raise ResearchQARuntimeError(
                "sweep runner must return StrategySweepResult"
            )
    except BaseException as exc:
        failure = exc
    finally:
        if embedding is not None and not embedding_released:
            try:
                embedding.release_model()
                embedding_released = True
            except BaseException as exc:
                if failure is None:
                    failure = exc
        if reranker is not None:
            try:
                reranker.release_model()
            except BaseException as exc:
                if failure is None:
                    failure = exc

    if failure is not None:
        write_preflight(status="failed", error=failure)
        _atomic_write_json(
            summary_path,
            {
                "schema_version": RUNTIME_SCHEMA_VERSION,
                "status": "failed",
                "run_root": str(root),
                "inputs": input_summary,
                "model_preflight_path": str(preflight_path.resolve()),
                "error": {
                    "type": type(failure).__name__,
                    "message": str(failure),
                },
            },
        )
        if isinstance(failure, (KeyboardInterrupt, SystemExit)):
            raise failure
        if isinstance(failure, ResearchQARuntimeError):
            raise failure
        raise ResearchQARuntimeError(
            f"ResearchQA runtime failed: {failure}"
        ) from failure

    assert sweep_result is not None
    write_preflight(
        status="completed" if reranker_preflighted else "embedding-only"
    )
    _atomic_write_json(
        summary_path,
        {
            "schema_version": RUNTIME_SCHEMA_VERSION,
            "status": "completed",
            "run_root": str(root),
            "inputs": input_summary,
            "model_cache": {
                "embeddings": str(embedding_cache_dir.resolve()),
                "huggingface": str(hf_cache_dir.resolve()),
            },
            "lifecycle": {
                "embedding_released": embedding_released,
                "embedding_cache_only": cache_only_entered,
                "reranker_preflighted": reranker_preflighted,
                "reranker_released": True,
            },
            "model_preflight_path": str(preflight_path.resolve()),
            "sweep": _sweep_summary(sweep_result),
        },
    )
    return ResearchQARuntimeResult(
        sweep_result=sweep_result,
        model_preflight_path=str(preflight_path.resolve()),
        runtime_summary_path=str(summary_path.resolve()),
    )


__all__ = [
    "EXPECTED_PAPER_COUNT",
    "EXPECTED_QUESTION_COUNT",
    "RUNTIME_SCHEMA_VERSION",
    "ResearchQARuntimeError",
    "ResearchQARuntimeResult",
    "load_frozen_notes",
    "load_suite_questions",
    "run_researchqa_runtime",
]
