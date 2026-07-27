from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from benchmarks.researchqa_retrieval import (
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
)
from benchmarks.researchqa_scoring import (
    MacroAggregate,
    evaluate_mapping_coverage,
    map_reference_groups,
)
from benchmarks.researchqa_strategy import (
    CandidateRunResult,
    EvidenceMappingBundle,
    QuestionStrategyResult,
)
from benchmarks.researchqa_sweep import (
    SweepContractError,
    run_strategy_sweep,
)
from service.pdf_ir import (
    CanonicalDocument,
    DEFAULT_EXTRACTOR_FINGERPRINT,
    DocumentPage,
    hash_text,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _config():
    value = yaml.safe_load(
        (REPO_ROOT / "benchmarks" / "configs" / "rq2-overnight.yaml")
        .read_text(encoding="utf-8")
    )
    value["benchmark"]["paper_count"] = 1
    value["benchmark"]["question_count"] = 1
    return value


def _documents():
    page = DocumentPage.create(
        paper_id="W1",
        file_id="Main",
        pdf_page_index=0,
        text="Alpha evidence.",
    )
    return {
        "W1": CanonicalDocument(
            paper_id="W1",
            file_id="Main",
            file_hash=hash_text("source-W1"),
            extractor_fingerprint=DEFAULT_EXTRACTOR_FINGERPRINT,
            pages=(page,),
        )
    }


QUESTIONS = (
    {
        "row_id": "q1",
        "paper_id": "https://openalex.org/W1",
        "domain": "domain-a",
        "question_type": "lookup",
        "question": "What is the Alpha evidence?",
        "expected_references": [{"alternatives": ["Alpha evidence."]}],
    },
)


class _FakeEmbedder:
    model_id = "qwen3-embedding:4b"
    model_digest = "d" * 64
    dimensions = 2
    normalization_revision = "test-v1"

    def __init__(self, events):
        self.events = events

    def embed_texts(self, texts):
        self.events.append("embed")
        return [(1.0, 0.1) for _ in texts]


class _FakeReranker:
    model_id = RERANKER_MODEL_ID
    revision = RERANKER_REVISION

    def __init__(self, events):
        self.events = events

    def score_pairs(self, query, passages, *, batch_size):
        self.events.append("rerank")
        return [1.0 for _ in passages]


def _primary(candidate) -> float:
    stage_scores = {
        "pdf-chunker": {
            "pdf-fixed-400": 0.99,
            "pdf-fixed-800": 0.98,
            "pdf-fixed-1200": 0.97,
            "pdf-page-aware": 0.96,
            "pdf-section-aware": 0.95,
            "pdf-structure-aware": 0.94,
            "pdf-parent-child": 0.93,
        },
        "note-chunker": {
            "note-section": 0.99,
            "note-claim-evidence": 0.98,
            "note-reviewer-concern": 0.97,
            "note-whole": 0.80,
        },
        "retriever": {
            "dense": 0.99,
            "bm25": 0.98,
            "hybrid-rrf": 0.97,
        },
        "source-composition": {
            "pdf-only": 0.99,
            "pdf-note-rrf": 0.98,
            "note-to-pdf": 0.97,
            "note-guided-pdf": 0.96,
            "hierarchical-pdf": 0.95,
        },
        "reranker": {
            "rerank-50-to-10": 0.99,
            "rerank-100-to-10": 0.98,
            "rerank-20-to-10": 0.97,
            "rerank-off": 0.96,
        },
    }
    if candidate.stage_id == "pdf-chunker":
        return stage_scores["pdf-chunker"][candidate.pdf_chunker]
    if candidate.stage_id == "note-chunker":
        return stage_scores["note-chunker"][candidate.note_chunker]
    if candidate.stage_id == "retriever":
        return stage_scores["retriever"][candidate.retriever]
    if candidate.stage_id == "source-composition":
        return stage_scores["source-composition"][
            candidate.source_composition
        ]
    if candidate.stage_id == "reranker":
        return stage_scores["reranker"][candidate.reranker]
    return (
        stage_scores["pdf-chunker"][candidate.pdf_chunker]
        + stage_scores["retriever"][candidate.retriever]
        + stage_scores["source-composition"][candidate.source_composition]
        + stage_scores["reranker"][candidate.reranker]
    ) / 4


def _candidate_result(candidate, *, incomplete=False, alternate_group=False):
    mapping = map_reference_groups(
        row_id="q1",
        paper_id="W1",
        domain="domain-a",
        question_type="lookup",
        reference_groups=[{"alternatives": ["Alpha evidence."]}],
        mapper=lambda _text: "chunk-alpha",
    )
    if alternate_group:
        changed_group = replace(mapping.groups[0], group_id="different-group")
        mapping = replace(mapping, groups=(changed_group,))
    coverage = evaluate_mapping_coverage((mapping,))
    bundle = EvidenceMappingBundle(
        revision="test-mapping-v1",
        fuzzy_threshold=0.86,
        mappings=(mapping,),
        unmapped=(),
        coverage=coverage,
    )
    primary = _primary(candidate)
    metrics = {
        "coverage_ndcg_at_10": primary,
        "recall_at_5": 1.0,
        "recall_at_10": 1.0,
        "mrr": 1.0,
        "all_required_groups_success_at_5": 1.0,
        "all_required_groups_success_at_10": 1.0,
        "groups_covered_at_10": 1.0,
    }
    aggregate = MacroAggregate(
        overall=metrics,
        by_domain={"domain-a": metrics},
        by_paper={"W1": metrics},
        by_question_type={"lookup": metrics},
    )
    question_result = QuestionStrategyResult(
        row_id="q1",
        paper_id="W1",
        domain="domain-a",
        question_type="lookup",
        ranked_item_ids=("chunk-alpha",),
        metrics=metrics,
    )
    return CandidateRunResult(
        candidate=candidate,
        question_results=(question_result,),
        aggregate=aggregate,
        mapping=bundle,
        completed_paper_ids=() if incomplete else ("W1",),
        completed_question_ids=("q1",),
        p95_latency_ms=10.0,
        index_bytes=100,
        chunk_count=1,
        guardrails_passed=True,
    )


class _FakeExecutor:
    def __init__(
        self,
        *,
        interrupt_at=None,
        incomplete_pdf=False,
        mismatched_evaluable=False,
    ):
        self.interrupt_at = interrupt_at
        self.incomplete_pdf = incomplete_pdf
        self.mismatched_evaluable = mismatched_evaluable
        self.calls = []

    def __call__(
        self,
        candidate,
        _documents,
        _questions,
        *,
        embedder,
        reranker,
        **_kwargs,
    ):
        self.calls.append(candidate.config_id)
        if self.interrupt_at == len(self.calls):
            raise KeyboardInterrupt("simulated interruption")
        embedder.embed_texts((candidate.config_id,))
        if candidate.reranker != "rerank-off":
            reranker.score_pairs("q", ("p",), batch_size=1)
        return _candidate_result(
            candidate,
            incomplete=(
                self.incomplete_pdf
                and candidate.stage_id == "pdf-chunker"
                and candidate.pdf_chunker == "pdf-fixed-1200"
            ),
            alternate_group=(
                self.mismatched_evaluable
                and candidate.stage_id == "pdf-chunker"
                and candidate.pdf_chunker == "pdf-fixed-1200"
            ),
        )


def _run(
    tmp_path,
    executor,
    events,
):
    return run_strategy_sweep(
        _config(),
        tmp_path,
        _documents(),
        QUESTIONS,
        {"W1": "# Frozen note\n\nAlpha evidence. [Main p.1]"},
        _FakeEmbedder(events),
        _FakeReranker(events),
        before_rerank_stage=lambda: events.append("before-rerank"),
        assert_embedding_cache_only=lambda candidate: events.append(
            f"cache-only:{candidate.stage_id}"
        ),
        candidate_executor=executor,
    )


def test_interrupted_sweep_resumes_39_unique_candidates_and_orders_callbacks(
    tmp_path,
):
    first_events = []
    with pytest.raises(KeyboardInterrupt, match="simulated"):
        _run(
            tmp_path,
            _FakeExecutor(interrupt_at=5, incomplete_pdf=True),
            first_events,
        )
    assert len(list((tmp_path / "sweep" / "candidates").rglob("*.json"))) == 4

    events = []
    executor = _FakeExecutor(incomplete_pdf=True)
    result = _run(tmp_path, executor, events)

    assert len(result.records) == 39
    assert len({record.candidate.config_id for record in result.records}) == 39
    assert sum(record.resumed for record in result.records) == 4
    assert len(executor.calls) == 35
    assert len(list((tmp_path / "sweep" / "candidates").rglob("*.json"))) == 39
    assert not list((tmp_path / "sweep").rglob("*.tmp"))

    assert events.count("before-rerank") == 1
    before = events.index("before-rerank")
    cache_events = [
        (index, event)
        for index, event in enumerate(events)
        if event.startswith("cache-only:")
    ]
    assert len(cache_events) == 20
    assert all(index > before for index, _event in cache_events)
    assert all(
        any(
            later == "embed"
            for later in events[index + 1 :]
        )
        for index, _event in cache_events
    )

    pdf_ranking = result.stage_rankings["pdf-chunker"]
    incomplete = next(
        record
        for record in result.records
        if record.candidate.stage_id == "pdf-chunker"
        and record.candidate.pdf_chunker == "pdf-fixed-1200"
    )
    assert incomplete.candidate.config_id in pdf_ranking.incomplete_config_ids
    assert incomplete.candidate.config_id not in {
        record.candidate.config_id for record in pdf_ranking.ranked
    }
    assert result.provisional_winner
    assert len(result.leaderboard) == 16
    assert result.pareto_frontier
    for stage_id in (
        "pdf-chunker",
        "note-chunker",
        "retriever",
        "source-composition",
        "reranker",
        "top2-confirmation",
    ):
        stage_root = tmp_path / "sweep" / "stages" / stage_id
        assert (stage_root / "ranking.json").is_file()
        assert (stage_root / "unmapped.json").is_file()
        assert (stage_root / "completeness.json").is_file()


def test_bad_payload_sha_reexecutes_only_the_corrupt_candidate(tmp_path):
    result = _run(tmp_path, _FakeExecutor(), [])
    record = result.records[0]
    path = Path(record.result_path)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["payload"]["primary_score"] = -1
    path.write_text(json.dumps(envelope), encoding="utf-8")

    executor = _FakeExecutor()
    resumed = _run(tmp_path, executor, [])

    assert len(executor.calls) == 1
    assert sum(record.resumed for record in resumed.records) == 38
    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["payload"]["primary_score"] > 0


def test_stage_ranking_rejects_different_evaluable_sets(tmp_path):
    with pytest.raises(SweepContractError, match="same evaluable set"):
        _run(
            tmp_path,
            _FakeExecutor(mismatched_evaluable=True),
            [],
        )
    stage_root = tmp_path / "sweep" / "stages" / "pdf-chunker"
    ranking = json.loads(
        (stage_root / "ranking.json").read_text(encoding="utf-8")
    )
    assert ranking["status"] == "blocked"
    assert (stage_root / "unmapped.json").is_file()
    assert (stage_root / "completeness.json").is_file()
