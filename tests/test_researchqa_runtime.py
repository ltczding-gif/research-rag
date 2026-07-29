from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from benchmarks.researchqa_models import (
    OLLAMA_EMBED_DIMENSIONS,
    OLLAMA_EMBED_MODEL_DIGEST,
    OLLAMA_EMBED_MODEL_ID,
)
from benchmarks.researchqa_chunking import (
    PDF_STRUCTURE_FALLBACK_ID,
    chunk_pdf,
    structure_fallback_corpus_diagnostics,
)
from benchmarks.researchqa_notes import GENERIC_TEMPLATE
from benchmarks.researchqa_retrieval import (
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
)
from benchmarks.researchqa_strategy import (
    R1_RETRIEVER_FUSION_POLICY,
    RR1_RERANK_FUSION_POLICY,
    S1_SOURCE_FUSION_POLICY,
    audit_n1_note_route,
)
from benchmarks.researchqa_runtime import (
    ResearchQARuntimeError,
    run_n0_n3_prequality_runtime,
    run_researchqa_extension_runtime,
    run_researchqa_runtime,
)
from benchmarks.researchqa_sweep import (
    StrategySweepResult,
    SweepCandidateRecord,
)


def _sha(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _write_jsonl(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(
            json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
            for row in rows
        ),
        encoding="utf-8",
    )


def _write_native_ir(run_root: Path, paper_id: str) -> None:
    text = " ".join(
        (
            f"Evidence for {paper_id} appears on this Main page.",
            "The methods, observations, controls, and limitations are retained",
            "so deterministic chunking has enough indexable text for runtime",
            "contract tests without relying on an external PDF parser.",
        )
    )
    _write_jsonl(
        run_root / "source" / paper_id / "native-ir.jsonl",
        [
            {
                "schema_version": 1,
                "unit_id": f"native-{paper_id}-1",
                "paper_id": paper_id,
                "file_id": "Main",
                "source_role": "benchmark_pdf",
                "media_type": "application/pdf",
                "source_sha256": _sha(f"source:{paper_id}"),
                "parser_fingerprint": _sha("runtime-test-parser-v1"),
                "ordinal": 1,
                "coordinate": {
                    "coordinate_type": "pdf_page",
                    "page": 1,
                },
                "citation": "[Main p.1]",
                "text": text,
                "text_sha256": _sha(text),
            }
        ],
    )


def _runtime_fixture(tmp_path: Path) -> tuple[dict[str, object], Path]:
    cache_root = tmp_path / "cache"
    run_root = tmp_path / "run"
    paper_ids = [f"W{index}" for index in range(1, 21)]
    questions = []
    for index in range(254):
        paper_id = paper_ids[index % len(paper_ids)]
        questions.append(
            {
                "row_id": f"q-{index + 1:03d}",
                "paper_id": f"https://openalex.org/{paper_id}",
                "domain": f"domain-{index % 10}",
                "question_type": "lookup",
                "question": f"What evidence belongs to {paper_id}?",
                "expected_references": [],
            }
        )
    _write_jsonl(
        cache_root / "suites" / "rq-2" / "questions.jsonl",
        questions,
    )

    frozen_rows = []
    for paper_id in paper_ids:
        _write_native_ir(run_root, paper_id)
        note = f"""# {paper_id}

## Findings
### C1：Bounded claim for {paper_id}
Evidence E1 is retained. [Main p.1]

## 审稿人视角（Adaptive Red-Team Verdict）
| Claim | 裁决 | 证据充分度 | 最强替代解释 | 决定性缺失证据 | 严重性 |
|---|---|---|---|---|---|
| C1 | bounded | E1 [Main p.1] | alternative | test | minor |
"""
        note_path = (
            run_root
            / "note-runs"
            / "frozen"
            / "notes"
            / f"{paper_id}.md"
        )
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_text(note, encoding="utf-8")
        frozen_rows.append(
            {
                "schema_version": 1,
                "paper_id": paper_id,
                "template": GENERIC_TEMPLATE,
                "note_sha256": _sha(note_path.read_bytes()),
            }
        )
    _write_jsonl(
        run_root / "note-runs" / "frozen" / "frozen-notes.jsonl",
        frozen_rows,
    )

    config: dict[str, object] = {
        "benchmark": {
            "benchmark_id": "researchqa",
            "tier_id": "rq-2",
            "paper_count": 20,
            "question_count": 254,
        },
        "retrieval": {"scope": "paper-scoped"},
        "paths": {
            "cache_root": str(cache_root),
            "suite_dir": "suites/rq-2",
        },
        "models": {
            "embedding": {
                "model": OLLAMA_EMBED_MODEL_ID,
                "digest": OLLAMA_EMBED_MODEL_DIGEST,
                "dimensions": OLLAMA_EMBED_DIMENSIONS,
            },
            "reranker": {
                "model": RERANKER_MODEL_ID,
                "revision": RERANKER_REVISION,
            },
        },
    }
    return config, run_root


def _add_extension_contract(config: dict[str, object]) -> None:
    config["gates"] = {
        "mapping_overall_minimum": 0.95,
        "mapping_per_paper_minimum": 0.90,
        "relative_guardrails": {
            "max_domain_regression": 0.02,
            "max_regressed_domains": 1,
            "max_question_type_regression": 0.02,
            "max_overall_guardrail_regression": 0.005,
            "max_new_recall_at_10_hard_failures": 0,
        },
    }
    config["metrics"] = {
        "guardrails": [
            "recall_at_5",
            "recall_at_10",
            "mrr",
            "all_required_groups_success_at_5",
            "all_required_groups_success_at_10",
            "groups_covered_at_10",
        ],
    }
    config["performance"] = {
        "sample_question_count": 40,
        "warmup_passes": 1,
        "timed_passes": 3,
    }
    config["stages"] = {
        "pdf_chunkers": [
            {"id": value}
            for value in (
                "pdf-fixed-400",
                "pdf-fixed-800",
                "pdf-fixed-1200",
                "pdf-page-aware",
                "pdf-section-aware",
                "pdf-structure-aware",
                "pdf-parent-child",
            )
        ],
        "note_chunkers": [
            {"id": "note-whole", "rankable": False},
            {"id": "note-section", "rankable": True},
            {"id": "note-claim-evidence", "rankable": True},
            {"id": "note-reviewer-concern", "rankable": False},
        ],
        "retrievers": [
            {"id": "dense"},
            {"id": "bm25"},
            {"id": "hybrid-rrf"},
        ],
        "source_compositions": [
            {"id": "pdf-only"},
            {"id": "note-to-pdf"},
            {"id": "pdf-note-rrf"},
            {"id": "note-guided-pdf"},
            {"id": "hierarchical-pdf"},
        ],
        "rerankers": [
            {"id": "rerank-off", "enabled": False},
            {
                "id": "rerank-20-to-10",
                "enabled": True,
                "input_k": 20,
                "output_k": 10,
            },
            {
                "id": "rerank-50-to-10",
                "enabled": True,
                "input_k": 50,
                "output_k": 10,
            },
            {
                "id": "rerank-100-to-10",
                "enabled": True,
                "input_k": 100,
                "output_k": 10,
            },
        ],
    }


def _extension_record(
    kwargs: dict[str, object],
    *,
    status: str = "completed",
) -> SweepCandidateRecord:
    documents = kwargs["documents"]
    if kwargs["extension_id"] == "F2":
        diagnostic_key = "pdf_chunking"
        diagnostics = structure_fallback_corpus_diagnostics(
            {
                paper_id: chunk_pdf(
                    document,
                    PDF_STRUCTURE_FALLBACK_ID,
                    is_main=True,
                )
                for paper_id, document in documents.items()
            }
        )
    elif kwargs["extension_id"] == "RR1":
        diagnostic_key = "rerank_fusion"
        diagnostics = dict(RR1_RERANK_FUSION_POLICY)
    elif kwargs["extension_id"] == "R1":
        diagnostic_key = "retriever_fusion"
        diagnostics = dict(R1_RETRIEVER_FUSION_POLICY)
    else:
        diagnostic_key = "source_fusion"
        diagnostics = dict(S1_SOURCE_FUSION_POLICY)
    candidate = kwargs["candidate"]
    questions = kwargs["questions"]
    question_ids = [str(question["row_id"]) for question in questions]
    evaluable_set = [
        [row_id, f"group-{index:03d}-a"]
        for index, row_id in enumerate(question_ids[:239])
    ]
    evaluable_set.extend(
        [row_id, f"group-{index:03d}-b"]
        for index, row_id in enumerate(question_ids[:141])
    )
    payload = {
        "execution_complete": status == "completed",
        "guardrail_finalized": status == "completed",
        "guardrails_passed": status == "completed",
        "primary_score": 0.75,
        "completed_paper_ids": sorted(documents),
        "completed_question_ids": question_ids,
        "mapping": {
            "coverage": {"passed": status == "completed"},
            "evaluable_set": evaluable_set,
        },
        "chunk_count": int(diagnostics.get("output_chunk_count", 20)),
        "corpus_diagnostics": {
            diagnostic_key: diagnostics,
            **(
                {
                    "note_route": audit_n1_note_route(
                        candidate,
                        documents,
                        kwargs["frozen_notes"],
                    )
                }
                if kwargs["extension_id"] == "S1"
                else {}
            ),
        },
        "extension": {
            "extension_id": kwargs["extension_id"],
            "baseline_config_id": kwargs["baseline_candidate"].config_id,
        },
    }
    return SweepCandidateRecord(
        candidate=candidate,
        status=status,
        input_fingerprint="synthetic-extension-input",
        payload=payload,
        result_path="synthetic-extension-result.json",
    )


def test_n0_n3_prequality_runs_without_models_and_writes_diagnostics(
    tmp_path: Path,
) -> None:
    config, run_root = _runtime_fixture(tmp_path)
    _add_extension_contract(config)

    result = run_n0_n3_prequality_runtime(config, run_root)

    assert result.candidate_config_id.startswith("repair-n1-")
    assert result.diagnostics["eligible_paper_ids"] == sorted(
        f"W{index}" for index in range(1, 21)
    )
    assert result.diagnostics["fallback_paper_ids"] == []
    assert result.diagnostics["base_chunk_count"] == 20
    assert result.diagnostics["backlinkable_base_chunk_count"] == 20
    assert result.diagnostics["reviewer_chunk_count"] == 0
    assert result.diagnostics["reviewer_verdict_row_count"] == 20
    assert result.diagnostics["reviewer_severity_counts"] == {
        "fatal": 0,
        "major": 0,
        "minor": 20,
        "zero": 0,
    }
    payload = json.loads(
        Path(result.prequality_path).read_text(encoding="utf-8")
    )
    assert payload["status"] == "completed"
    assert payload["inputs"]["paper_count"] == 20
    assert payload["inputs"]["question_count"] == 254


@pytest.mark.parametrize("failure_mode", ["missing", "sha-mismatch"])
def test_runtime_rejects_missing_or_unbound_frozen_note(
    tmp_path: Path,
    failure_mode: str,
) -> None:
    config, run_root = _runtime_fixture(tmp_path)
    note_path = run_root / "note-runs" / "frozen" / "notes" / "W7.md"
    if failure_mode == "missing":
        note_path.unlink()
        expected = "does not exist"
    else:
        note_path.write_text("tampered", encoding="utf-8")
        expected = "SHA-256 mismatch"

    def forbidden_factory(**_kwargs: object) -> object:
        raise AssertionError("models must not be constructed for invalid notes")

    with pytest.raises(ResearchQARuntimeError, match=expected):
        run_researchqa_runtime(
            config,
            run_root,
            embedding_factory=forbidden_factory,
            reranker_factory=forbidden_factory,
        )

    runtime_root = run_root / "runtime"
    summary = json.loads(
        (runtime_root / "runtime-summary.json").read_text(encoding="utf-8")
    )
    preflight = json.loads(
        (runtime_root / "model-preflight.json").read_text(encoding="utf-8")
    )
    assert summary["status"] == "failed"
    assert preflight["status"] == "failed"
    assert preflight["embedding"] is None
    assert preflight["reranker"] is None


def test_runtime_enforces_model_lifecycle_and_writes_final_artifacts(
    tmp_path: Path,
) -> None:
    config, run_root = _runtime_fixture(tmp_path)
    events: list[str] = []
    instances: dict[str, object] = {}

    class FakeEmbedding:
        def __init__(self, *, cache_dir: Path):
            self.cache_dir = Path(cache_dir)
            self._cache_only = False
            events.append("embedding:init")

        def preflight(self) -> dict[str, object]:
            events.append("embedding:preflight")
            return {
                "provider": "fake-ollama",
                "model_id": OLLAMA_EMBED_MODEL_ID,
                "fingerprint": "embedding-fingerprint",
            }

        def release_model(self) -> bool:
            events.append("embedding:release")
            return True

        def enter_cache_only(self) -> None:
            events.append("embedding:cache-only")
            self._cache_only = True

    class FakeReranker:
        def __init__(self, *, hf_home: Path, device: str):
            self.hf_home = Path(hf_home)
            self.device = device
            events.append("reranker:init")

        def preflight(self) -> dict[str, object]:
            events.append("reranker:preflight")
            return {
                "provider": "fake-transformers",
                "model_id": RERANKER_MODEL_ID,
                "fingerprint": "reranker-fingerprint",
            }

        def release_model(self) -> bool:
            events.append("reranker:release")
            return True

    def embedding_factory(**kwargs: object) -> FakeEmbedding:
        instance = FakeEmbedding(**kwargs)
        instances["embedding"] = instance
        return instance

    def reranker_factory(**kwargs: object) -> FakeReranker:
        instance = FakeReranker(**kwargs)
        instances["reranker"] = instance
        return instance

    def fake_sweep(**kwargs: object) -> StrategySweepResult:
        events.append("sweep:start")
        assert len(kwargs["documents"]) == 20
        assert len(kwargs["questions"]) == 254
        assert len(kwargs["frozen_notes"]) == 20
        kwargs["before_rerank_stage"]()
        events.append("sweep:assert-cache-only")
        kwargs["assert_embedding_cache_only"](object())
        final_path = Path(kwargs["run_root"]) / "sweep" / "final" / "decision.json"
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_text('{"status":"completed"}\n', encoding="utf-8")
        return StrategySweepResult(
            records=(),
            stage_rankings={"top2-confirmation": object()},
            provisional_winner="winner-config",
            leaderboard=({"config_id": "winner-config"},),
            pareto_frontier=({"config_id": "winner-config"},),
            artifact_paths=(str(final_path.resolve()),),
        )

    result = run_researchqa_runtime(
        config,
        run_root,
        embedding_factory=embedding_factory,
        reranker_factory=reranker_factory,
        sweep_runner=fake_sweep,
    )

    assert events == [
        "embedding:init",
        "reranker:init",
        "embedding:preflight",
        "sweep:start",
        "embedding:release",
        "embedding:cache-only",
        "reranker:preflight",
        "sweep:assert-cache-only",
        "reranker:release",
    ]
    embedding = instances["embedding"]
    reranker = instances["reranker"]
    assert embedding.cache_dir == run_root / "model-cache" / "embeddings"
    assert reranker.hf_home == run_root / "model-cache" / "hf-cache"
    assert reranker.device == "cuda"

    preflight_path = Path(result.model_preflight_path)
    summary_path = Path(result.runtime_summary_path)
    decision_path = Path(result.sweep_result.artifact_paths[0])
    assert preflight_path.is_file()
    assert summary_path.is_file()
    assert decision_path.is_file()

    preflight = json.loads(preflight_path.read_text(encoding="utf-8"))
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert preflight["status"] == "completed"
    assert preflight["embedding"]["fingerprint"] == "embedding-fingerprint"
    assert preflight["reranker"]["fingerprint"] == "reranker-fingerprint"
    assert summary["status"] == "completed"
    assert summary["inputs"]["paper_count"] == 20
    assert summary["inputs"]["question_count"] == 254
    assert summary["lifecycle"] == {
        "embedding_released": True,
        "embedding_cache_only": True,
        "reranker_preflighted": True,
        "reranker_released": True,
    }
    assert summary["sweep"]["provisional_winner"] == "winner-config"
    assert summary["sweep"]["artifact_paths"] == [str(decision_path)]


def test_runtime_fails_closed_and_releases_reranker_on_sweep_error(
    tmp_path: Path,
) -> None:
    config, run_root = _runtime_fixture(tmp_path)
    events: list[str] = []

    class FakeEmbedding:
        _cache_only = False

        def __init__(self, **_kwargs: object):
            pass

        def preflight(self) -> dict[str, str]:
            events.append("embedding:preflight")
            return {"fingerprint": "embedding"}

        def release_model(self) -> bool:
            events.append("embedding:release")
            return True

        def enter_cache_only(self) -> None:
            events.append("embedding:cache-only")
            self._cache_only = True

    class FakeReranker:
        def __init__(self, **_kwargs: object):
            pass

        def preflight(self) -> dict[str, str]:
            events.append("reranker:preflight")
            return {"fingerprint": "reranker"}

        def release_model(self) -> bool:
            events.append("reranker:release")
            return True

    def failing_sweep(**kwargs: object) -> StrategySweepResult:
        kwargs["before_rerank_stage"]()
        kwargs["assert_embedding_cache_only"](object())
        raise ValueError("synthetic sweep failure")

    with pytest.raises(
        ResearchQARuntimeError,
        match="synthetic sweep failure",
    ):
        run_researchqa_runtime(
            config,
            run_root,
            embedding_factory=FakeEmbedding,
            reranker_factory=FakeReranker,
            sweep_runner=failing_sweep,
        )

    assert events == [
        "embedding:preflight",
        "embedding:release",
        "embedding:cache-only",
        "reranker:preflight",
        "reranker:release",
    ]
    summary = json.loads(
        (run_root / "runtime" / "runtime-summary.json").read_text(
            encoding="utf-8"
        )
    )
    assert summary["status"] == "failed"
    assert summary["error"]["type"] == "ValueError"
    assert summary["error"]["message"] == "synthetic sweep failure"


def test_extension_runtime_runs_f2_without_loading_reranker(
    tmp_path: Path,
) -> None:
    config, run_root = _runtime_fixture(tmp_path)
    _add_extension_contract(config)
    events: list[str] = []

    class FakeEmbedding:
        model_id = OLLAMA_EMBED_MODEL_ID
        model_digest = OLLAMA_EMBED_MODEL_DIGEST
        dimensions = OLLAMA_EMBED_DIMENSIONS
        normalization_revision = "exact-text-utf8-v1"

        def __init__(self, *, cache_dir: Path):
            self.cache_dir = Path(cache_dir)
            events.append("embedding:init")

        def preflight(self) -> dict[str, object]:
            events.append("embedding:preflight")
            return {
                "provider": "fake-ollama",
                "model_id": self.model_id,
                "fingerprint": "extension-embedding",
            }

        def release_model(self) -> bool:
            events.append("embedding:release")
            return True

    def fake_extension(**kwargs: object) -> SweepCandidateRecord:
        events.append("extension:run")
        assert kwargs["extension_id"] == "F2"
        assert kwargs["reranker"] is None
        assert kwargs["candidate"].pdf_chunker == PDF_STRUCTURE_FALLBACK_ID
        assert kwargs["baseline_candidate"].pdf_chunker == "pdf-fixed-1200"
        return _extension_record(kwargs)

    result = run_researchqa_extension_runtime(
        config,
        run_root,
        extension_id="F2",
        embedding_factory=FakeEmbedding,
        extension_runner=fake_extension,
    )

    assert events == [
        "embedding:init",
        "embedding:preflight",
        "extension:run",
        "embedding:release",
    ]
    assert result.record.status == "completed"
    assert result.record.guardrail_finalized is True
    preflight = json.loads(
        Path(result.model_preflight_path).read_text(encoding="utf-8")
    )
    prequality = json.loads(
        Path(result.prequality_path).read_text(encoding="utf-8")
    )
    summary = json.loads(
        Path(result.runtime_summary_path).read_text(encoding="utf-8")
    )
    assert preflight["status"] == "completed"
    assert preflight["reranker"]["required"] is False
    assert prequality["status"] == "completed"
    assert prequality["diagnostics"]["paper_count"] == 20
    assert summary["status"] == "completed"
    assert summary["lifecycle"] == {
        "embedding_released": True,
        "embedding_cache_only": False,
        "reranker_required": False,
        "reranker_released": False,
    }
    assert summary["result"]["completed_paper_count"] == 20
    assert summary["result"]["completed_question_count"] == 254
    assert summary["result"]["evaluable_question_count"] == 239
    assert summary["result"]["mapped_group_count"] == 380


def test_extension_runtime_runs_rr1_in_embedding_cache_only_mode(
    tmp_path: Path,
) -> None:
    config, run_root = _runtime_fixture(tmp_path)
    _add_extension_contract(config)
    events: list[str] = []

    class FakeEmbedding:
        def __init__(self, *, cache_dir: Path):
            self.cache_dir = Path(cache_dir)
            self._cache_only = False
            events.append("embedding:init")

        def preflight(self) -> dict[str, str]:
            events.append("embedding:preflight")
            return {"fingerprint": "rr1-embedding"}

        def release_model(self) -> bool:
            events.append("embedding:release")
            return True

        def enter_cache_only(self) -> None:
            self._cache_only = True
            events.append("embedding:cache-only")

    class FakeReranker:
        def __init__(self, *, hf_home: Path, device: str):
            assert Path(hf_home).name == "hf-cache"
            assert device == "cuda"
            events.append("reranker:init")

        def preflight(self) -> dict[str, str]:
            events.append("reranker:preflight")
            return {"fingerprint": "rr1-reranker"}

        def release_model(self) -> bool:
            events.append("reranker:release")
            return True

    def fake_extension(**kwargs: object) -> SweepCandidateRecord:
        assert kwargs["extension_id"] == "RR1"
        assert kwargs["reranker"].__class__ is FakeReranker
        kwargs["before_rerank_stage"]()
        kwargs["assert_embedding_cache_only"](kwargs["candidate"])
        assert kwargs["embedder"]._cache_only is True
        assert kwargs["candidate"].rerank_fusion is not None
        assert kwargs["baseline_candidate"].reranker == "rerank-off"
        events.append("extension:run")
        return _extension_record(kwargs)

    result = run_researchqa_extension_runtime(
        config,
        run_root,
        extension_id="RR1",
        embedding_factory=FakeEmbedding,
        reranker_factory=FakeReranker,
        extension_runner=fake_extension,
    )

    assert events == [
        "embedding:init",
        "embedding:preflight",
        "reranker:init",
        "embedding:release",
        "embedding:cache-only",
        "reranker:preflight",
        "extension:run",
        "reranker:release",
    ]
    preflight = json.loads(
        Path(result.model_preflight_path).read_text(encoding="utf-8")
    )
    summary = json.loads(
        Path(result.runtime_summary_path).read_text(encoding="utf-8")
    )
    assert preflight["status"] == "completed"
    assert preflight["reranker"]["fingerprint"] == "rr1-reranker"
    assert summary["lifecycle"] == {
        "embedding_released": True,
        "embedding_cache_only": True,
        "reranker_required": True,
        "reranker_released": True,
    }
    assert summary["result"]["corpus_diagnostics"] == {
        "rerank_fusion": dict(RR1_RERANK_FUSION_POLICY)
    }


def test_extension_runtime_runs_r1_without_loading_reranker(
    tmp_path: Path,
) -> None:
    config, run_root = _runtime_fixture(tmp_path)
    _add_extension_contract(config)
    events: list[str] = []

    class FakeEmbedding:
        model_id = OLLAMA_EMBED_MODEL_ID
        model_digest = OLLAMA_EMBED_MODEL_DIGEST
        dimensions = OLLAMA_EMBED_DIMENSIONS
        normalization_revision = "exact-text-utf8-v1"

        def __init__(self, *, cache_dir: Path):
            self.cache_dir = Path(cache_dir)
            events.append("embedding:init")

        def preflight(self) -> dict[str, object]:
            events.append("embedding:preflight")
            return {
                "provider": "fake-ollama",
                "model_id": self.model_id,
                "fingerprint": "r1-embedding",
            }

        def release_model(self) -> bool:
            events.append("embedding:release")
            return True

    def fake_extension(**kwargs: object) -> SweepCandidateRecord:
        events.append("extension:run")
        assert kwargs["extension_id"] == "R1"
        assert kwargs["reranker"] is None
        assert kwargs["candidate"].retriever_fusion is not None
        assert kwargs["baseline_candidate"].retriever == "dense"
        return _extension_record(kwargs)

    result = run_researchqa_extension_runtime(
        config,
        run_root,
        extension_id="R1",
        embedding_factory=FakeEmbedding,
        extension_runner=fake_extension,
    )

    assert events == [
        "embedding:init",
        "embedding:preflight",
        "extension:run",
        "embedding:release",
    ]
    summary = json.loads(
        Path(result.runtime_summary_path).read_text(encoding="utf-8")
    )
    assert summary["lifecycle"] == {
        "embedding_released": True,
        "embedding_cache_only": False,
        "reranker_required": False,
        "reranker_released": False,
    }
    assert summary["result"]["corpus_diagnostics"] == {
        "retriever_fusion": dict(R1_RETRIEVER_FUSION_POLICY)
    }


def test_extension_runtime_runs_s1_with_verified_n1_prequality(
    tmp_path: Path,
) -> None:
    config, run_root = _runtime_fixture(tmp_path)
    _add_extension_contract(config)
    events: list[str] = []

    class FakeEmbedding:
        model_id = OLLAMA_EMBED_MODEL_ID
        model_digest = OLLAMA_EMBED_MODEL_DIGEST
        dimensions = OLLAMA_EMBED_DIMENSIONS
        normalization_revision = "exact-text-utf8-v1"

        def __init__(self, *, cache_dir: Path):
            self.cache_dir = Path(cache_dir)
            events.append("embedding:init")

        def preflight(self) -> dict[str, object]:
            events.append("embedding:preflight")
            return {
                "provider": "fake-ollama",
                "model_id": self.model_id,
                "fingerprint": "s1-embedding",
            }

        def release_model(self) -> bool:
            events.append("embedding:release")
            return True

    def fake_extension(**kwargs: object) -> SweepCandidateRecord:
        events.append("extension:run")
        assert kwargs["extension_id"] == "S1"
        assert kwargs["reranker"] is None
        assert kwargs["candidate"].source_fusion is not None
        assert kwargs["baseline_candidate"].source_composition == "pdf-only"
        return _extension_record(kwargs)

    result = run_researchqa_extension_runtime(
        config,
        run_root,
        extension_id="S1",
        embedding_factory=FakeEmbedding,
        extension_runner=fake_extension,
    )

    assert events == [
        "embedding:init",
        "embedding:preflight",
        "extension:run",
        "embedding:release",
    ]
    prequality = json.loads(
        Path(result.prequality_path).read_text(encoding="utf-8")
    )
    summary = json.loads(
        Path(result.runtime_summary_path).read_text(encoding="utf-8")
    )
    assert prequality["note_route"]["fallback_paper_ids"] == []
    assert len(prequality["note_route"]["eligible_paper_ids"]) == 20
    assert summary["result"]["corpus_diagnostics"]["source_fusion"] == dict(
        S1_SOURCE_FUSION_POLICY
    )
    assert summary["result"]["corpus_diagnostics"]["note_route"][
        "fallback_paper_ids"
    ] == []


def test_extension_runtime_fails_closed_on_incomplete_record(
    tmp_path: Path,
) -> None:
    config, run_root = _runtime_fixture(tmp_path)
    _add_extension_contract(config)
    events: list[str] = []

    class FakeEmbedding:
        def __init__(self, **_kwargs: object):
            pass

        def preflight(self) -> dict[str, str]:
            return {"fingerprint": "extension-embedding"}

        def release_model(self) -> bool:
            events.append("embedding:release")
            return True

    def incomplete_extension(**kwargs: object) -> SweepCandidateRecord:
        return _extension_record(kwargs, status="failed")

    with pytest.raises(
        ResearchQARuntimeError,
        match="did not complete the frozen input set",
    ):
        run_researchqa_extension_runtime(
            config,
            run_root,
            extension_id="F2",
            embedding_factory=FakeEmbedding,
            extension_runner=incomplete_extension,
        )

    assert events == ["embedding:release"]
    summary = json.loads(
        (
            run_root
            / "sweep"
            / "extensions"
            / "F2"
            / "runtime"
            / "runtime-summary.json"
        ).read_text(encoding="utf-8")
    )
    assert summary["status"] == "failed"
    assert summary["error"]["type"] == "ResearchQARuntimeError"
