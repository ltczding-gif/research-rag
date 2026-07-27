from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from benchmarks.researchqa_chunking import chunk_pdf
from benchmarks.researchqa_retrieval import (
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
)
from benchmarks.researchqa_strategy import (
    ConfirmationSelection,
    REFERENCE_MATCH_REVISION,
    StrategyContractError,
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
        fuzzy_threshold=0.90,
        overall_minimum=0.0,
        per_paper_minimum=0.0,
    )
    groups = bundle.mappings[0].groups

    assert groups[0].alternatives[0].match_method == "nfkc-whitespace-exact-v1"
    assert groups[1].alternatives[0].match_method == REFERENCE_MATCH_REVISION
    assert groups[0].mapped and groups[1].mapped
    assert not groups[2].mapped
    assert bundle.unmapped[0].alternatives == (
        "Completely unrelated lunar evidence.",
    )


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
    expected_papers = ("W1", "W2")
    expected_questions = ("q-alpha", "q-beta", "q-diagnostic")

    result = run_complete_candidate(
        base,
        documents,
        questions,
        expected_paper_ids=expected_papers,
        expected_question_ids=expected_questions,
        embedder=embedder,
    )
    reranked = run_complete_candidate(
        enabled_rerank,
        documents,
        questions,
        expected_paper_ids=expected_papers,
        expected_question_ids=expected_questions,
        embedder=embedder,
        reranker=reranker,
    )

    assert result.is_complete(
        expected_paper_ids=expected_papers,
        expected_question_ids=expected_questions,
    )
    assert len(result.question_results) == 3
    by_id = {item.row_id: item for item in result.question_results}
    assert by_id["q-alpha"].metrics["recall_at_5"] == 1.0
    assert by_id["q-beta"].metrics["recall_at_5"] == 1.0
    assert by_id["q-diagnostic"].metrics["recall_at_5"] is None
    assert result.primary_metric == "recall_at_5"
    assert result.primary_score == pytest.approx(1.0)
    assert result.chunk_count == 2
    assert result.index_bytes > 0
    assert result.latency_metrics["measurement_revision"] == (
        "stratified-warm-query-v1"
    )
    assert result.latency_metrics["performance_question_count"] == 3
    assert result.latency_metrics["sample_count"] == 9
    assert result.latency_metrics["query_p95_ms"] >= 0
    assert result.latency_metrics["rerank_p95_ms"] == 0
    assert result.p95_latency_ms == result.latency_metrics["p95_latency_ms"]
    assert embedder.calls
    assert reranker.calls
    assert reranked.primary_score == pytest.approx(1.0)
    assert reranked.primary_metric == "coverage_ndcg_at_10"
    assert reranked.latency_metrics["rerank_p95_ms"] >= 0


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
    }
    fast = replace(
        result,
        candidate=replace(candidate, config_id="fast"),
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
        "fast",
        candidate.config_id,
    ]
    assert ranking.incomplete_config_ids == ("incomplete",)
    assert ranking.ineligible_config_ids == ("diagnostic",)
