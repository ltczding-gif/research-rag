from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

import benchmarks.researchqa_strategy as strategy
from benchmarks.researchqa_chunking import (
    NOTE_CLAIM_PLUS_REVIEWER_ID,
    PDF_STRUCTURE_FALLBACK_ID,
    PDF_STRUCTURE_FALLBACK_POLICY,
    chunk_pdf,
)
from benchmarks.researchqa_retrieval import (
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
)
from benchmarks.researchqa_strategy import (
    ConfirmationSelection,
    NOTE_ROUTE_ELIGIBILITY_POLICY,
    REFERENCE_EXACT_METHOD,
    REFERENCE_MATCH_REVISION,
    REFERENCE_PAGE_HINT_METHOD,
    REFERENCE_SECTION_HINT_METHOD,
    StrategyContractError,
    generate_f2_candidate,
    generate_n1_candidate,
    generate_orthogonal_candidates,
    load_main_documents,
    map_all_references,
    rank_stage_results,
    run_complete_candidate,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write_native_ir(run_root: Path, paper_id: str, page_texts: list[str]) -> None:
    path = run_root / "source" / paper_id / "native-ir.jsonl"
    path.parent.mkdir(parents=True)
    rows = []
    source_hash = _sha(f"source:{paper_id}")
    parser_fingerprint = _sha("parser-v1")
    for page, text in enumerate(page_texts, 1):
        rows.append(
            {
                "schema_version": 1,
                "unit_id": f"native-{paper_id}-{page}",
                "paper_id": paper_id,
                "file_id": "Main",
                "source_role": "benchmark_pdf",
                "media_type": "application/pdf",
                "source_sha256": source_hash,
                "parser_fingerprint": parser_fingerprint,
                "ordinal": page,
                "coordinate": {
                    "coordinate_type": "pdf_page",
                    "page": page,
                },
                "citation": f"[Main p.{page}]",
                "text": text,
                "text_sha256": _sha(text),
            }
        )
    path.write_text(
        "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows),
        encoding="utf-8",
    )


def _fixture_corpus(tmp_path: Path):
    alpha = (
        "Alpha result was 42 units under the controlled condition. "
        + "Alpha mechanism evidence remains stable. " * 8
    )
    beta = (
        "Beta result was 17 units under the comparison condition. "
        + "Beta mechanism evidence remains stable. " * 8
    )
    _write_native_ir(tmp_path, "W1", [alpha])
    _write_native_ir(tmp_path, "W2", [beta])
    documents = load_main_documents(
        tmp_path,
        expected_paper_ids=(
            "https://openalex.org/W1",
            "https://openalex.org/W2",
        ),
    )
    questions = [
        {
            "row_id": "q-alpha",
            "paper_id": "https://openalex.org/W1",
            "domain": "domain-a",
            "question_type": "lookup",
            "question": "What was the Alpha result?",
            "expected_references": [
                {
                    "alternatives": [
                        "Ａlpha  result was 42 units under the "
                        "controlled condition."
                    ]
                }
            ],
        },
        {
            "row_id": "q-beta",
            "paper_id": "https://openalex.org/W2",
            "domain": "domain-b",
            "question_type": "multi_hop",
            "question": "What was the Beta result?",
            "expected_references": [
                {
                    "alternatives": [
                        "Beta result was 17 units under the comparison "
                        "condition."
                    ]
                }
            ],
        },
        {
            "row_id": "q-diagnostic",
            "paper_id": "https://openalex.org/W1",
            "domain": "domain-a",
            "question_type": "adversarial",
            "question": "Which unsupported claim should be refused?",
            "expected_references": [],
        },
    ]
    return documents, questions


def _n1_note(
    label: str,
    *,
    base_citation: str,
    reviewer_severity: str = "minor",
    reviewer_citation: str = "[Main p.1]",
) -> str:
    return f"""# Frozen note

## Findings
### C1：{label} claim
The evidence chain uses E1. {base_citation}

## 审稿人视角（Adaptive Red-Team Verdict）
| Claim | 裁决 | 证据充分度 | 最强替代解释 | 决定性缺失证据 | 严重性 |
|---|---|---|---|---|---|
| C1 | bounded | E1 {reviewer_citation} | alternative | test | {reviewer_severity} |
"""


class _FakeEmbedder:
    def __init__(self):
        self.calls = []

    def embed_texts(self, texts):
        self.calls.append(tuple(texts))
        vectors = []
        for text in texts:
            normalized = unicodedata.normalize("NFKC", text).lower()
            if "alpha" in normalized:
                vectors.append((1.0, 0.05))
            elif "beta" in normalized:
                vectors.append((0.05, 1.0))
            else:
                vectors.append((0.7, 0.7))
        return vectors


class _FakeReranker:
    model_id = RERANKER_MODEL_ID
    revision = RERANKER_REVISION

    def __init__(self):
        self.calls = []

    def score_pairs(self, query, passages, *, batch_size):
        self.calls.append((query, tuple(passages), batch_size))
        token = "alpha" if "Alpha" in query else "beta"
        return [
            1.0 if token in passage.lower() else 0.0
            for passage in passages
        ]


def _config():
    return yaml.safe_load(
        (REPO_ROOT / "benchmarks" / "configs" / "rq2-overnight.yaml")
        .read_text(encoding="utf-8")
    )


def test_loads_only_main_pdf_native_ir_as_canonical_documents(tmp_path):
    _write_native_ir(tmp_path, "W1", ["page one", "page two"])
    documents = load_main_documents(
        tmp_path,
        expected_paper_ids=["https://openalex.org/W1"],
    )

    document = documents["W1"]
    assert document.file_id == "Main"
    assert [page.pdf_page_index for page in document.pages] == [0, 1]
    assert [page.normalized_text for page in document.pages] == [
        "page one",
        "page two",
    ]

    path = tmp_path / "source" / "W1" / "native-ir.jsonl"
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    rows[0]["text_sha256"] = "0" * 64
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    with pytest.raises(StrategyContractError, match="text hash mismatch"):
        load_main_documents(tmp_path)


def test_reference_mapping_uses_nfkc_exact_then_versioned_fuzzy(tmp_path):
    documents, _ = _fixture_corpus(tmp_path)
    chunks = chunk_pdf(
        documents["W1"],
        "pdf-fixed-800",
        is_main=True,
    ).chunks
    questions = [
        {
            "row_id": "q-map",
            "paper_id": "W1",
            "domain": "d",
            "question_type": "multi_hop",
            "question": "q",
            "expected_references": [
                {
                    "alternatives": [
                        "Ａlpha   result was 42 units under the "
                        "controlled condition."
                    ]
                },
                {
                    "alternatives": [
                        "Alpha result was 43 units under the "
                        "controlled condition."
                    ]
                },
                {"alternatives": ["Completely unrelated lunar evidence."]},
            ],
        }
    ]

    bundle = map_all_references(
        questions,
        chunks,
        documents=documents,
        fuzzy_threshold=0.90,
        overall_minimum=0.0,
        per_paper_minimum=0.0,
    )
    groups = bundle.mappings[0].groups

    assert groups[0].alternatives[0].match_method == REFERENCE_EXACT_METHOD
    assert groups[1].alternatives[0].match_method == REFERENCE_MATCH_REVISION
    assert groups[0].mapped and groups[1].mapped
    assert not groups[2].mapped
    assert bundle.unmapped[0].alternatives == (
        "Completely unrelated lunar evidence.",
    )


def test_page_aligned_reference_mapping_is_deterministic(tmp_path):
    documents, questions = _fixture_corpus(tmp_path)
    chunks = tuple(
        chunk
        for document in documents.values()
        for chunk in chunk_pdf(
            document,
            "pdf-fixed-400",
            is_main=True,
        ).chunks
    )

    first = map_all_references(
        questions,
        chunks,
        documents=documents,
        overall_minimum=0.0,
        per_paper_minimum=0.0,
    )
    second = map_all_references(
        questions,
        chunks,
        documents=documents,
        overall_minimum=0.0,
        per_paper_minimum=0.0,
    )

    assert second.to_dict() == first.to_dict()


def test_reference_mapping_uses_researchqa_page_hint_for_version_drift(
    tmp_path,
):
    documents, _ = _fixture_corpus(tmp_path)
    chunks = chunk_pdf(
        documents["W1"],
        "pdf-fixed-400",
        is_main=True,
    ).chunks
    question = {
        "row_id": "q-page-hint",
        "paper_id": "W1",
        "domain": "d",
        "question_type": "lookup",
        "question": "q",
        "metadata_page_hint": 1,
        "expected_references": [
            {
                "section_label": "Results",
                "alternatives": ["A wording found only in another edition."],
            }
        ],
    }

    bundle = map_all_references(
        [question],
        chunks,
        documents={"W1": documents["W1"]},
        overall_minimum=0.0,
        per_paper_minimum=0.0,
    )
    alternative = bundle.mappings[0].groups[0].alternatives[0]

    assert alternative.mapped
    assert alternative.match_method == REFERENCE_PAGE_HINT_METHOD


def test_reference_mapping_uses_section_hint_without_page_hint(tmp_path):
    _write_native_ir(
        tmp_path,
        "W1",
        [
            "Methods\n"
            + "Observed evidence in the available edition. " * 5
        ],
    )
    documents = load_main_documents(
        tmp_path,
        expected_paper_ids=["https://openalex.org/W1"],
    )
    chunks = chunk_pdf(
        documents["W1"],
        "pdf-fixed-400",
        is_main=True,
    ).chunks
    question = {
        "row_id": "q-section-hint",
        "paper_id": "W1",
        "domain": "d",
        "question_type": "adversarial",
        "question": "q",
        "expected_references": [
            {
                "section_label": "Methods",
                "alternatives": ["Wording from the published HTML edition."],
            }
        ],
    }

    bundle = map_all_references(
        [question],
        chunks,
        documents=documents,
        overall_minimum=0.0,
        per_paper_minimum=0.0,
    )
    alternative = bundle.mappings[0].groups[0].alternatives[0]

    assert alternative.mapped
    assert alternative.match_method == REFERENCE_SECTION_HINT_METHOD


def test_candidate_plan_is_orthogonal_and_confirmation_is_capped_at_16():
    selection = ConfirmationSelection(
        pdf_chunkers=("pdf-fixed-400", "pdf-fixed-800"),
        retrievers=("dense", "bm25"),
        source_compositions=("pdf-only", "pdf-note-rrf"),
        reranker_modes=("rerank-off", "rerank-50-to-10"),
    )
    plan = generate_orthogonal_candidates(_config(), confirmation=selection)

    assert {stage: len(candidates) for stage, candidates in plan.stages.items()} == {
        "pdf-chunker": 7,
        "note-chunker": 4,
        "retriever": 3,
        "source-composition": 5,
        "reranker": 4,
        "top2-confirmation": 16,
    }
    assert len(plan.candidates) == 39
    assert len({candidate.config_id for candidate in plan.candidates}) == 39
    note_whole = next(
        candidate
        for candidate in plan.stages["note-chunker"]
        if candidate.note_chunker == "note-whole"
    )
    assert not note_whole.rankable


def test_f2_candidate_is_independent_and_policy_bound():
    config = _config()
    original = generate_orthogonal_candidates(config)
    first = generate_f2_candidate(config)
    second = generate_f2_candidate(config)

    assert len(original.candidates) == 23
    assert all(
        candidate.pdf_chunker != PDF_STRUCTURE_FALLBACK_ID
        for candidate in original.candidates
    )
    assert first == second
    assert first.config_id.startswith("repair-f2-")
    assert first.stage_id == "pdf-chunker"
    assert first.pdf_chunker == PDF_STRUCTURE_FALLBACK_ID
    assert first.retriever == "dense"
    assert first.source_composition == "pdf-only"
    assert first.reranker == "rerank-off"
    assert (
        PDF_STRUCTURE_FALLBACK_POLICY["revision"]
        == "rq2-f2-structure-quality-v1"
    )


def test_n1_candidate_is_independent_and_policy_bound():
    config = _config()
    original = generate_orthogonal_candidates(config)
    first = generate_n1_candidate(config)
    second = generate_n1_candidate(config)

    assert len(original.candidates) == 23
    assert all(
        candidate.note_chunker != NOTE_CLAIM_PLUS_REVIEWER_ID
        for candidate in original.candidates
    )
    assert first == second
    assert first.config_id.startswith("repair-n1-")
    assert first.stage_id == "note-chunker"
    assert first.pdf_chunker == "pdf-fixed-1200"
    assert first.note_chunker == NOTE_CLAIM_PLUS_REVIEWER_ID
    assert first.retriever == "dense"
    assert first.source_composition == "pdf-note-rrf"
    assert first.reranker == "rerank-off"
    assert NOTE_ROUTE_ELIGIBILITY_POLICY["revision"] == (
        "rq2-n0-paper-note-route-eligibility-v1"
    )


def test_n0_eligibility_uses_only_backlinkable_claim_base_chunks(tmp_path):
    documents, _questions = _fixture_corpus(tmp_path)
    candidate = generate_n1_candidate(_config())
    notes = {
        "W1": _n1_note("Alpha", base_citation="[Main p.1]"),
        "W2": _n1_note(
            "Beta",
            base_citation="[SI p.1]",
            reviewer_severity="major",
            reviewer_citation="[Main p.1]",
        ),
    }

    corpus = strategy._prepare_candidate_corpus(
        candidate,
        documents,
        notes,
    )
    diagnostics = corpus.diagnostics["note_route"]

    assert corpus.eligible_note_paper_ids == ("W1",)
    assert corpus.fallback_note_paper_ids == ("W2",)
    assert diagnostics["eligible_paper_ids"] == ["W1"]
    assert diagnostics["fallback_paper_ids"] == ["W2"]
    assert diagnostics["base_chunk_count"] == 2
    assert diagnostics["reviewer_chunk_count"] == 1
    assert diagnostics["backlinkable_base_chunk_count"] == 1
    assert diagnostics["backlinkable_reviewer_chunk_count"] == 1
    assert diagnostics["per_paper"]["W2"]["eligible"] is False
    assert (
        diagnostics["per_paper"]["W2"][
            "backlinkable_reviewer_chunk_count"
        ]
        == 1
    )
    assert len(diagnostics["diagnostic_fingerprint"]) == 64


def test_n0_fallback_is_exact_direct_pdf_even_with_reviewer_backlinks(
    tmp_path,
    monkeypatch,
):
    documents, questions = _fixture_corpus(tmp_path)
    documents = {"W2": documents["W2"]}
    questions = [
        question
        for question in questions
        if question["row_id"] == "q-beta"
    ]
    expected_papers = ("W2",)
    expected_questions = ("q-beta",)
    n1 = generate_n1_candidate(_config())
    baseline = next(
        candidate
        for candidate in generate_orthogonal_candidates(_config()).stages[
            "pdf-chunker"
        ]
        if candidate.pdf_chunker == "pdf-fixed-1200"
    )
    note = _n1_note(
        "Beta",
        base_citation="[SI p.1]",
        reviewer_severity="major",
        reviewer_citation="[Main p.1]",
    )

    baseline_result = run_complete_candidate(
        baseline,
        documents,
        questions,
        expected_paper_ids=expected_papers,
        expected_question_ids=expected_questions,
        embedder=_FakeEmbedder(),
    )
    monkeypatch.setattr(
        strategy,
        "pdf_note_rrf",
        lambda *_args, **_kwargs: pytest.fail(
            "N0 fallback must bypass note RRF"
        ),
    )
    n1_result = run_complete_candidate(
        n1,
        documents,
        questions,
        expected_paper_ids=expected_papers,
        expected_question_ids=expected_questions,
        embedder=_FakeEmbedder(),
        notes={"W2": note},
    )

    baseline_row = baseline_result.question_results[0]
    n1_row = n1_result.question_results[0]
    assert n1_row.ranked_item_ids == baseline_row.ranked_item_ids
    assert n1_row.ranked_scores == baseline_row.ranked_scores
    assert n1_row.metrics == baseline_row.metrics
    assert n1_result.corpus_diagnostics["note_route"][
        "fallback_paper_ids"
    ] == ["W2"]


def test_f2_corpus_records_global_diagnostics_and_fails_over_cost(
    tmp_path,
    monkeypatch,
):
    documents, _questions = _fixture_corpus(tmp_path)
    candidate = generate_f2_candidate(_config())

    corpus = strategy._prepare_candidate_corpus(
        candidate,
        documents,
        notes=None,
    )
    diagnostics = corpus.diagnostics["pdf_chunking"]
    assert diagnostics["paper_count"] == 2
    assert diagnostics["contract_status"] == "passed"
    assert all(
        chunk.config_id == PDF_STRUCTURE_FALLBACK_ID
        for chunk in corpus.pdf_chunks
    )

    monkeypatch.setattr(
        strategy,
        "structure_fallback_corpus_diagnostics",
        lambda _results: {
            "contract_status": "failed",
            "output_to_fixed_1200_ratio": 1.3,
        },
    )
    with pytest.raises(
        StrategyContractError,
        match="F2 global output cost 1.300000 exceeds 1.250000",
    ):
        strategy._prepare_candidate_corpus(
            candidate,
            documents,
            notes=None,
        )


def test_confirmation_deduplicates_hierarchical_pdf_compatibility_aliases():
    selection = ConfirmationSelection(
        pdf_chunkers=("pdf-fixed-400", "pdf-fixed-800"),
        retrievers=("dense", "bm25"),
        source_compositions=("pdf-only", "hierarchical-pdf"),
        reranker_modes=("rerank-off", "rerank-50-to-10"),
    )

    plan = generate_orthogonal_candidates(_config(), confirmation=selection)
    candidates = plan.stages["top2-confirmation"]

    assert len(candidates) == 12
    assert len({candidate.config_id for candidate in candidates}) == 12
    hierarchical = [
        candidate
        for candidate in candidates
        if candidate.source_composition == "hierarchical-pdf"
    ]
    assert len(hierarchical) == 4
    assert {
        candidate.pdf_chunker for candidate in hierarchical
    } == {"pdf-parent-child"}
    assert len(plan.candidates) == 35


def test_complete_candidate_uses_fake_models_and_scores_every_question(tmp_path):
    documents, questions = _fixture_corpus(tmp_path)
    plan = generate_orthogonal_candidates(_config())
    base = next(
        candidate
        for candidate in plan.stages["pdf-chunker"]
        if candidate.pdf_chunker == "pdf-fixed-800"
    )
    enabled_rerank = next(
        candidate
        for candidate in plan.stages["reranker"]
        if candidate.reranker == "rerank-20-to-10"
    )
    embedder = _FakeEmbedder()
    reranker = _FakeReranker()
    evidence_mapping_cache = {}
    expected_papers = ("W1", "W2")
    expected_questions = ("q-alpha", "q-beta", "q-diagnostic")

    result = run_complete_candidate(
        base,
        documents,
        questions,
        expected_paper_ids=expected_papers,
        expected_question_ids=expected_questions,
        embedder=embedder,
        evidence_mapping_cache=evidence_mapping_cache,
    )
    reranked = run_complete_candidate(
        enabled_rerank,
        documents,
        questions,
        expected_paper_ids=expected_papers,
        expected_question_ids=expected_questions,
        embedder=embedder,
        reranker=reranker,
        evidence_mapping_cache=evidence_mapping_cache,
    )

    assert result.is_complete(
        expected_paper_ids=expected_papers,
        expected_question_ids=expected_questions,
    )
    assert len(result.question_results) == 3
    by_id = {item.row_id: item for item in result.question_results}
    chunks_by_paper = {
        paper_id: {
            chunk.chunk_id
            for chunk in chunk_pdf(
                document,
                base.pdf_chunker,
                is_main=True,
            ).chunks
        }
        for paper_id, document in documents.items()
    }
    assert set(by_id["q-alpha"].ranked_item_ids) <= chunks_by_paper["W1"]
    assert set(by_id["q-beta"].ranked_item_ids) <= chunks_by_paper["W2"]
    assert set(by_id["q-diagnostic"].ranked_item_ids) <= chunks_by_paper["W1"]
    assert by_id["q-alpha"].metrics["recall_at_5"] == 1.0
    assert by_id["q-beta"].metrics["recall_at_5"] == 1.0
    assert by_id["q-diagnostic"].metrics["recall_at_5"] is None
    assert result.primary_metric == "recall_at_5"
    assert result.primary_score == pytest.approx(1.0)
    assert result.retrieval_scope == "paper-scoped"
    assert all(item.ranked_scores for item in result.question_results)
    assert result.chunk_count == 2
    assert result.index_bytes > 0
    assert result.latency_metrics["measurement_revision"] == (
        "stratified-warm-query-v2"
    )
    assert result.latency_metrics["performance_question_count"] == 3
    assert result.latency_metrics["sample_count"] == 9
    assert result.latency_metrics["query_p95_ms"] >= 0
    assert result.latency_metrics["rerank_p95_ms"] == 0
    assert result.p95_latency_ms == result.latency_metrics["p95_latency_ms"]
    assert embedder.calls
    assert reranker.calls
    assert {call[2] for call in reranker.calls} == {1}
    assert reranked.primary_score == pytest.approx(1.0)
    assert reranked.primary_metric == "coverage_ndcg_at_10"
    assert reranked.latency_metrics["rerank_p95_ms"] >= 0
    assert reranked.latency_metrics["reranker_batch_size"] == 1
    assert all(
        item.pre_rerank_item_ids
        and len(item.pre_rerank_item_ids) == len(item.pre_rerank_scores)
        and {
            "recall_at_20",
            "recall_at_50",
            "recall_at_100",
        } <= set(item.pre_rerank_metrics)
        for item in reranked.question_results
    )
    assert len(evidence_mapping_cache) == 1
    assert reranked.mapping is result.mapping


def test_complete_candidate_resumes_quality_at_complete_paper_boundary(tmp_path):
    documents, questions = _fixture_corpus(tmp_path)
    candidate = next(
        item
        for item in generate_orthogonal_candidates(_config()).stages[
            "reranker"
        ]
        if item.reranker == "rerank-20-to-10"
    )
    expected_papers = ("W1", "W2")
    expected_questions = ("q-alpha", "q-beta", "q-diagnostic")
    snapshots = []

    class _FailOnBeta(_FakeReranker):
        def score_pairs(self, query, passages, *, batch_size):
            if "Beta" in query:
                raise RuntimeError("quality interruption")
            return super().score_pairs(
                query,
                passages,
                batch_size=batch_size,
            )

    with pytest.raises(RuntimeError, match="quality interruption") as raised:
        run_complete_candidate(
            candidate,
            documents,
            questions,
            expected_paper_ids=expected_papers,
            expected_question_ids=expected_questions,
            embedder=_FakeEmbedder(),
            reranker=_FailOnBeta(),
            p95_latency_ms=1.0,
            progress_callback=lambda value: snapshots.append(
                json.loads(json.dumps(value))
            ),
        )

    assert raised.value.researchqa_failure_context == {
        "phase": "quality",
        "paper_id": "W2",
        "row_id": "q-beta",
        "pass_kind": None,
        "pass_index": None,
    }
    resume = snapshots[-1]
    assert resume["phase"] == "quality"
    assert resume["completed_paper_ids"] == ["W1"]
    assert resume["completed_question_ids"] == [
        "q-alpha",
        "q-diagnostic",
    ]

    reranker = _FakeReranker()
    result = run_complete_candidate(
        candidate,
        documents,
        questions,
        expected_paper_ids=expected_papers,
        expected_question_ids=expected_questions,
        embedder=_FakeEmbedder(),
        reranker=reranker,
        p95_latency_ms=1.0,
        resume_progress=resume,
        progress_callback=lambda value: snapshots.append(
            json.loads(json.dumps(value))
        ),
    )

    assert [call[0] for call in reranker.calls] == [
        "What was the Beta result?"
    ]
    assert result.completed_paper_ids == expected_papers
    assert result.completed_question_ids == tuple(sorted(expected_questions))
    assert snapshots[-1]["phase"] == "aggregate"


def test_complete_candidate_resumes_only_complete_latency_passes(tmp_path):
    documents, questions = _fixture_corpus(tmp_path)
    candidate = next(
        item
        for item in generate_orthogonal_candidates(_config()).stages[
            "reranker"
        ]
        if item.reranker == "rerank-20-to-10"
    )
    expected_papers = ("W1", "W2")
    expected_questions = ("q-alpha", "q-beta", "q-diagnostic")
    snapshots = []

    class _FailOnEighthCall(_FakeReranker):
        def score_pairs(self, query, passages, *, batch_size):
            if len(self.calls) == 7:
                raise RuntimeError("timed pass interruption")
            return super().score_pairs(
                query,
                passages,
                batch_size=batch_size,
            )

    with pytest.raises(RuntimeError, match="timed pass interruption") as raised:
        run_complete_candidate(
            candidate,
            documents,
            questions,
            expected_paper_ids=expected_papers,
            expected_question_ids=expected_questions,
            embedder=_FakeEmbedder(),
            reranker=_FailOnEighthCall(),
            performance_warmup_passes=1,
            performance_timed_passes=2,
            progress_callback=lambda value: snapshots.append(
                json.loads(json.dumps(value))
            ),
        )

    assert raised.value.researchqa_failure_context == {
        "phase": "latency",
        "paper_id": "W1",
        "row_id": "q-alpha",
        "pass_kind": "timed",
        "pass_index": 0,
    }
    resume = snapshots[-1]
    assert resume["warmup_completed_passes"] == 1
    assert resume["timed_passes"] == []

    reranker = _FakeReranker()
    result = run_complete_candidate(
        candidate,
        documents,
        questions,
        expected_paper_ids=expected_papers,
        expected_question_ids=expected_questions,
        embedder=_FakeEmbedder(),
        reranker=reranker,
        performance_warmup_passes=1,
        performance_timed_passes=2,
        resume_progress=resume,
        progress_callback=lambda value: snapshots.append(
            json.loads(json.dumps(value))
        ),
    )

    assert len(reranker.calls) == 6
    assert result.latency_metrics["sample_count"] == 6
    assert snapshots[-1]["phase"] == "aggregate"
    assert len(snapshots[-1]["timed_passes"]) == 2


def test_reviewer_concern_note_chunker_is_diagnostic_not_rankable():
    candidates = generate_orthogonal_candidates(_config()).stages["note-chunker"]
    reviewer = next(
        candidate
        for candidate in candidates
        if candidate.note_chunker == "note-reviewer-concern"
    )

    assert reviewer.rankable is False


def test_note_strategy_fails_closed_without_frozen_notes(tmp_path):
    documents, questions = _fixture_corpus(tmp_path)
    candidate = generate_orthogonal_candidates(_config()).stages[
        "note-chunker"
    ][1]

    with pytest.raises(StrategyContractError, match="frozen notes are required"):
        run_complete_candidate(
            candidate,
            documents,
            questions,
            expected_paper_ids=("W1", "W2"),
            expected_question_ids=("q-alpha", "q-beta", "q-diagnostic"),
            embedder=_FakeEmbedder(),
        )


def test_stage_ranking_excludes_incomplete_and_diagnostic_candidates(tmp_path):
    documents, questions = _fixture_corpus(tmp_path)
    candidate = next(
        item
        for item in generate_orthogonal_candidates(_config()).stages[
            "pdf-chunker"
        ]
        if item.pdf_chunker == "pdf-fixed-800"
    )
    expected_papers = ("W1", "W2")
    expected_questions = ("q-alpha", "q-beta", "q-diagnostic")
    result = run_complete_candidate(
        candidate,
        documents,
        questions,
        expected_paper_ids=expected_papers,
        expected_question_ids=expected_questions,
        embedder=_FakeEmbedder(),
        p95_latency_ms=20,
    )
    assert result.latency_metrics == {
        "measurement_revision": "external-override",
        "p95_latency_ms": 20.0,
        "validity": "observed-only",
        "validity_reason": "external-override",
    }
    fast = replace(
        result,
        candidate=replace(candidate, config_id="zz-fast"),
        p95_latency_ms=10,
    )
    incomplete = replace(
        result,
        candidate=replace(candidate, config_id="incomplete"),
        completed_paper_ids=("W1",),
    )
    diagnostic = replace(
        result,
        candidate=replace(
            candidate,
            config_id="diagnostic",
            rankable=False,
        ),
    )

    ranking = rank_stage_results(
        "pdf-chunker",
        (result, fast, incomplete, diagnostic),
        expected_paper_ids=expected_papers,
        expected_question_ids=expected_questions,
    )

    assert [item.candidate.config_id for item in ranking.ranked] == [
        candidate.config_id,
        "zz-fast",
    ]
    assert ranking.incomplete_config_ids == ("incomplete",)
    assert ranking.ineligible_config_ids == ("diagnostic",)
