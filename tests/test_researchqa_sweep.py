from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from benchmarks.researchqa_models import ModelTransportError
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
    generate_orthogonal_candidates,
)
from benchmarks.researchqa_sweep import (
    SweepCandidateRecord,
    SweepContractError,
    _candidate_input_fingerprint,
    _relative_guardrail_diagnostics,
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
    inference_dtype = "bfloat16"
    adapter_revision = "fake-reranker-v1"

    def __init__(self, events):
        self.events = events

    def score_pairs(self, query, passages, *, batch_size):
        self.events.append("rerank")
        return [1.0 for _ in passages]


class _RevisedFakeReranker(_FakeReranker):
    adapter_revision = "fake-reranker-v2"


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


class _RegressingRerankerExecutor(_FakeExecutor):
    def __call__(self, candidate, *args, **kwargs):
        result = super().__call__(candidate, *args, **kwargs)
        if candidate.reranker == "rerank-off":
            return result
        metrics = {
            **result.aggregate.overall,
            "recall_at_10": 0.0,
            "all_required_groups_success_at_10": 0.0,
        }
        aggregate = replace(
            result.aggregate,
            overall=metrics,
            by_domain={"domain-a": metrics},
            by_paper={"W1": metrics},
            by_question_type={"lookup": metrics},
        )
        question_result = replace(
            result.question_results[0],
            metrics=metrics,
        )
        return replace(
            result,
            aggregate=aggregate,
            question_results=(question_result,),
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


def test_sweep_rejects_non_paper_scoped_retrieval(tmp_path):
    config = _config()
    config["retrieval"]["scope"] = "global-corpus"

    with pytest.raises(
        SweepContractError,
        match="retrieval.scope must be exactly 'paper-scoped'",
    ):
        run_strategy_sweep(
            config,
            tmp_path,
            _documents(),
            QUESTIONS,
            {"W1": "# Frozen note\n\nAlpha evidence. [Main p.1]"},
            _FakeEmbedder([]),
            _FakeReranker([]),
            before_rerank_stage=lambda: None,
            assert_embedding_cache_only=lambda _candidate: None,
            candidate_executor=_FakeExecutor(),
        )


def test_candidate_fingerprint_scopes_reranker_adapter_revision():
    plan = generate_orthogonal_candidates(_config())
    rerank_off = next(
        candidate
        for candidate in plan.stages["reranker"]
        if candidate.reranker == "rerank-off"
    )
    rerank_enabled = next(
        candidate
        for candidate in plan.stages["reranker"]
        if candidate.reranker == "rerank-20-to-10"
    )
    common = {
        "config_fingerprint": "config-v1",
        "documents": _documents(),
        "questions": QUESTIONS,
        "notes": {"W1": "# Frozen note\n\nAlpha evidence. [Main p.1]"},
        "embedder": _FakeEmbedder([]),
    }

    off_v1 = _candidate_input_fingerprint(
        rerank_off,
        reranker=_FakeReranker([]),
        **common,
    )
    off_v2 = _candidate_input_fingerprint(
        rerank_off,
        reranker=_RevisedFakeReranker([]),
        **common,
    )
    enabled_v1 = _candidate_input_fingerprint(
        rerank_enabled,
        reranker=_FakeReranker([]),
        **common,
    )
    enabled_v2 = _candidate_input_fingerprint(
        rerank_enabled,
        reranker=_RevisedFakeReranker([]),
        **common,
    )

    assert off_v1 == off_v2
    assert enabled_v1 != enabled_v2


def test_guardrail_pass_is_pending_until_diagnostics_are_finalized():
    candidate = generate_orthogonal_candidates(_config()).stages[
        "pdf-chunker"
    ][0]
    provisional_payload = _candidate_result(candidate).to_dict()
    record = SweepCandidateRecord(
        candidate=candidate,
        status="completed",
        input_fingerprint="input",
        payload=provisional_payload,
        result_path="candidate.json",
    )

    assert record.guardrail_finalized is False
    assert record.guardrails_passed is False

    finalized = replace(
        record,
        payload={
            **provisional_payload,
            "guardrail_finalized": True,
            "guardrail_diagnostics": {"passed": True, "failures": []},
        },
    )
    assert finalized.guardrail_finalized is True
    assert finalized.guardrails_passed is True


def test_relative_guardrails_reject_slice_regressions_and_new_hard_failure():
    plan = generate_orthogonal_candidates(_config())
    baseline_candidate = next(
        candidate
        for candidate in plan.stages["reranker"]
        if candidate.reranker == "rerank-off"
    )
    candidate = next(
        candidate
        for candidate in plan.stages["reranker"]
        if candidate.reranker == "rerank-50-to-10"
    )
    baseline_payload = _candidate_result(baseline_candidate).to_dict()
    candidate_payload = _candidate_result(candidate).to_dict()
    baseline_payload["metric_bundle_complete"] = True
    candidate_payload["metric_bundle_complete"] = True
    for domain in ("domain-a", "domain-b"):
        baseline_payload["score_summary"]["by_domain"][domain] = {
            **baseline_payload["score_summary"]["overall"],
            "coverage_ndcg_at_10": 0.90,
        }
        candidate_payload["score_summary"]["by_domain"][domain] = {
            **candidate_payload["score_summary"]["overall"],
            "coverage_ndcg_at_10": 0.85,
        }
    for question_type in ("multi_hop", "adversarial"):
        baseline_payload["score_summary"]["by_question_type"][
            question_type
        ] = {
            **baseline_payload["score_summary"]["overall"],
            "coverage_ndcg_at_10": 0.90,
        }
        candidate_payload["score_summary"]["by_question_type"][
            question_type
        ] = {
            **candidate_payload["score_summary"]["overall"],
            "coverage_ndcg_at_10": 0.85,
        }
    candidate_payload["score_summary"]["overall"]["recall_at_10"] = 0.98
    candidate_payload["score_summary"]["overall"][
        "all_required_groups_success_at_10"
    ] = 0.98
    candidate_payload["question_results"][0]["metrics"][
        "recall_at_10"
    ] = 0.0
    baseline_record = SweepCandidateRecord(
        candidate=baseline_candidate,
        status="completed",
        input_fingerprint="baseline",
        payload=baseline_payload,
        result_path="baseline.json",
    )
    record = SweepCandidateRecord(
        candidate=candidate,
        status="completed",
        input_fingerprint="candidate",
        payload=candidate_payload,
        result_path="candidate.json",
    )

    diagnostics = _relative_guardrail_diagnostics(
        record,
        baseline_record,
        candidate_payload,
        baseline_payload,
        max_domain_regression=0.02,
        max_regressed_domains=1,
        max_question_type_regression=0.02,
        max_overall_guardrail_regression=0.005,
        max_new_recall_at_10_hard_failures=0,
    )

    assert diagnostics["passed"] is False
    assert set(diagnostics["failures"]) == {
        "too-many-domain-regressions",
        "multi_hop-regression",
        "adversarial-regression",
        "recall_at_10-regression",
        "all_required_groups_success_at_10-regression",
        "new-recall-at-10-hard-failures",
    }
    assert diagnostics["new_recall_at_10_hard_failure_ids"] == ["q1"]


def test_failed_enabled_rerankers_are_confirmed_but_cannot_win(tmp_path):
    result = _run(tmp_path, _RegressingRerankerExecutor(), [])

    reranker_ranking = result.stage_rankings["reranker"]
    assert {
        record.candidate.reranker
        for record in reranker_ranking.ranked
    } == {"rerank-off"}
    assert len(result.records) == 35
    winner = next(
        record
        for record in result.records
        if record.candidate.config_id == result.provisional_winner
    )
    assert winner.candidate.reranker == "rerank-off"
    decision = json.loads(
        (
            tmp_path / "sweep" / "final" / "decision-summary.json"
        ).read_text(encoding="utf-8")
    )
    assert decision["confirmation_diagnostic_fallbacks"][
        "reranker_modes"
    ] == ["rerank-50-to-10"]


def test_interrupted_sweep_resumes_35_unique_candidates_and_orders_callbacks(
    tmp_path,
):
    first_events = []
    with pytest.raises(KeyboardInterrupt, match="simulated"):
        _run(
            tmp_path,
            _FakeExecutor(interrupt_at=5),
            first_events,
        )
    assert len(list((tmp_path / "sweep" / "candidates").rglob("*.json"))) == 4

    events = []
    executor = _FakeExecutor()
    result = _run(tmp_path, executor, events)

    assert len(result.records) == 35
    assert len({record.candidate.config_id for record in result.records}) == 35
    assert sum(record.resumed for record in result.records) == 4
    assert len(executor.calls) == 31
    assert len(list((tmp_path / "sweep" / "candidates").rglob("*.json"))) == 35
    assert not list((tmp_path / "sweep").rglob("*.tmp"))

    assert events.count("before-rerank") == 1
    before = events.index("before-rerank")
    cache_events = [
        (index, event)
        for index, event in enumerate(events)
        if event.startswith("cache-only:")
    ]
    assert len(cache_events) == 16
    assert all(index > before for index, _event in cache_events)
    assert all(
        any(
            later == "embed"
            for later in events[index + 1 :]
        )
        for index, _event in cache_events
    )

    assert result.provisional_winner
    assert len(result.leaderboard) == 12
    assert result.pareto_frontier
    assert set(result.pareto_frontier[0]) == {
        "config_id",
        "stage_id",
        "primary",
        "p95_latency_ms",
        "index_bytes",
        "chunk_count",
        "status",
        "guardrails_passed",
        "rank",
    }
    report_root = tmp_path / "report"
    assert len(
        (report_root / "leaderboard.csv").read_text(
            encoding="utf-8"
        ).splitlines()
    ) == 36
    assert (
        report_root / "paper-domain-breakdown.csv"
    ).is_file()
    bootstrap = json.loads(
        (report_root / "paired-bootstrap.json").read_text(
            encoding="utf-8"
        )
    )
    assert bootstrap["metric"] == "coverage_ndcg_at_10"
    assert bootstrap["samples"] == 10_000
    assert bootstrap["confidence"] == 0.95
    assert (
        report_root / "blocked-and-unmapped.jsonl"
    ).is_file()
    assert "does not start rq-5" in (
        report_root / "morning-report.md"
    ).read_text(encoding="utf-8")
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
        assert (stage_root / "guardrails.json").is_file()


def test_transient_model_failure_propagates_without_failed_checkpoint(tmp_path):
    def executor(*_args, **_kwargs):
        raise ModelTransportError("temporary Ollama failure")

    with pytest.raises(ModelTransportError, match="temporary Ollama failure"):
        _run(tmp_path, executor, [])

    assert not list((tmp_path / "sweep" / "candidates").rglob("*.json"))


def test_incomplete_required_candidate_blocks_stage_and_final_export(tmp_path):
    with pytest.raises(
        SweepContractError,
        match="incomplete required candidates",
    ):
        _run(tmp_path, _FakeExecutor(incomplete_pdf=True), [])

    ranking = json.loads(
        (
            tmp_path
            / "sweep"
            / "stages"
            / "pdf-chunker"
            / "ranking.json"
        ).read_text(encoding="utf-8")
    )
    assert ranking["status"] == "blocked"
    assert ranking["incomplete_config_ids"]
    assert not (tmp_path / "sweep" / "final").exists()


def test_infrastructure_failure_is_diagnostic_and_blocks_final_export(tmp_path):
    class Executor(_FakeExecutor):
        def __call__(self, candidate, *args, **kwargs):
            if not self.calls:
                self.calls.append(candidate.config_id)
                raise SystemError("CUDA error return without exception set")
            return super().__call__(candidate, *args, **kwargs)

    with pytest.raises(
        SweepContractError,
        match="infrastructure/unknown failures",
    ):
        _run(tmp_path, Executor(), [])

    failed = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (tmp_path / "sweep" / "candidates").rglob("*.json")
        if json.loads(path.read_text(encoding="utf-8"))["status"] == "failed"
    ]
    assert len(failed) == 1
    payload = failed[0]["payload"]
    assert payload["failure_kind"] == "infrastructure"
    assert payload["execution_complete"] is False
    assert payload["guardrail_finalized"] is False
    assert payload["failure_context"] == {
        "phase": "candidate-execution",
        "row_id": None,
        "pass_index": None,
        "progress": {
            "completed_paper_ids": [],
            "completed_question_ids": [],
        },
    }
    assert "SystemError" in payload["traceback"]
    assert not (tmp_path / "sweep" / "final").exists()


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
    assert sum(record.resumed for record in resumed.records) == 34
    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["payload"]["primary_score"] > 0


def test_records_keep_verbose_candidate_rows_on_disk_only(tmp_path):
    result = _run(tmp_path, _FakeExecutor(), [])
    record = result.records[0]

    assert "question_results" not in record.payload
    assert "mappings" not in record.payload["mapping"]
    assert record.payload["paper_domains"] == {"W1": "domain-a"}

    envelope = json.loads(
        Path(record.result_path).read_text(encoding="utf-8")
    )
    assert envelope["payload"]["question_results"]
    assert envelope["payload"]["mapping"]["mappings"]


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
