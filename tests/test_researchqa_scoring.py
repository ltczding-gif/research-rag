from __future__ import annotations

import hashlib
import math

import pytest

from benchmarks.researchqa_scoring import (
    CandidateSummary,
    EvidenceContractError,
    EvidenceCoverageError,
    GoldEvidenceSpan,
    QuestionScore,
    enforce_mapping_coverage,
    evaluate_mapping_coverage,
    macro_aggregate,
    map_reference_groups,
    paired_bootstrap,
    rank_candidates,
    score_ranking,
)


def _verified(
    item_id: str,
    start: int,
    end: int,
    *,
    file_id: str = "file-1",
) -> dict[str, object]:
    text = "x" * (end - start)
    return {
        "mapped_item_ids": [item_id],
        "match_method": "exact-test-v1",
        "match_score": 1.0,
        "verification_state": "exact",
        "gold_version": "test-gold-v1",
        "gold_spans": [
            {
                "file_id": file_id,
                "file_hash": "a" * 64,
                "pdf_page_index": 0,
                "char_start_in_normalized_page": start,
                "char_end_in_normalized_page": end,
                "page_text_hash": "b" * 64,
                "evidence_text": text,
                "evidence_text_hash": hashlib.sha256(text.encode()).hexdigest(),
            }
        ],
    }


def _source(start: int, end: int, *, file_id: str = "file-1") -> dict[str, object]:
    return {
        "file_id": file_id,
        "file_hash": "a" * 64,
        "pdf_page_index": 0,
        "char_start_in_normalized_page": start,
        "char_end_in_normalized_page": end,
        "page_text_hash": "b" * 64,
    }


def _mapping(
    row_id: str,
    paper_id: str,
    domain: str,
    groups: list[dict],
    mapped: dict[str, object],
):
    return map_reference_groups(
        row_id=row_id,
        paper_id=paper_id,
        domain=domain,
        question_type="multi_hop",
        reference_groups=groups,
        mapper=lambda text: mapped.get(text),
    )


def test_evidence_groups_are_and_while_alternatives_are_or():
    mapping = _mapping(
        "q1",
        "p1",
        "biology",
        [
            {"alternatives": ["alpha", "alpha paraphrase"]},
            {"alternatives": ["beta", "beta paraphrase"]},
        ],
        {
            "alpha paraphrase": _verified("chunk-a", 0, 5),
            "beta": _verified("chunk-b", 10, 14),
        },
    )

    assert mapping.total_groups == 2
    assert mapping.mapped_groups == 2
    assert mapping.groups[0].mapped
    assert not mapping.groups[0].alternatives[0].mapped
    assert mapping.groups[0].alternatives[1].mapped

    sources = {
        "chunk-a": [_source(0, 5)],
        "chunk-b": [_source(10, 14)],
    }
    one_group = score_ranking(
        ["chunk-a", "noise"], mapping.groups, item_source_spans=sources
    )
    assert one_group.metrics["recall_at_5"] == 0.5
    assert one_group.metrics["all_required_groups_success_at_5"] == 0.0

    all_groups = score_ranking(
        ["chunk-a", "chunk-b"], mapping.groups, item_source_spans=sources
    )
    assert all_groups.metrics["recall_at_5"] == 1.0
    assert all_groups.metrics["all_required_groups_success_at_5"] == 1.0


def test_recall_mrr_coverage_ndcg_and_multi_hop():
    mapping = _mapping(
        "q2",
        "p1",
        "biology",
        [
            {"alternatives": ["a"]},
            {"alternatives": ["b"]},
        ],
        {"a": _verified("chunk-a", 0, 5), "b": _verified("chunk-b", 10, 14)},
    )
    metrics = score_ranking(
        ["noise", "chunk-a", "noise-2", "chunk-b"],
        mapping.groups,
        item_source_spans={
            "chunk-a": [_source(0, 5)],
            "chunk-b": [_source(10, 14)],
        },
    ).metrics

    assert metrics["recall_at_5"] == 1.0
    assert metrics["mrr"] == 0.5
    expected_ndcg = (
        1 / math.log2(2 + 1) + 1 / math.log2(4 + 1)
    ) / 2
    assert metrics["coverage_ndcg_at_10"] == pytest.approx(expected_ndcg)
    assert metrics["groups_covered_at_5"] == 2.0


def test_no_reference_question_is_diagnostic_not_primary_evaluable():
    metrics = score_ranking(["anything"], ())

    assert not metrics.evaluable
    assert metrics.metrics["coverage_ndcg_at_10"] is None
    assert metrics.metrics["recall_at_5"] is None


def test_strict_coverage_rejects_one_character_loose_hit():
    mapping = _mapping(
        "q-overlap",
        "p1",
        "biology",
        [{"alternatives": ["required span"]}],
        {"required span": _verified("touching-chunk", 90, 190)},
    )

    metrics = score_ranking(
        ["touching-chunk"],
        mapping.evaluable_groups,
        item_source_spans={"touching-chunk": [_source(0, 91)]},
    ).metrics

    assert metrics["coverage_ndcg_at_10"] == 0.0
    assert metrics["all_required_groups_success_at_10"] == 0.0
    assert metrics["loose_hit_coverage_ndcg_at_10"] == 1.0


def test_strict_coverage_unions_chunks_and_preserves_or_and_contract():
    mapping = _mapping(
        "q-union",
        "p1",
        "biology",
        [
            {"alternatives": ["long", "substitute"]},
            {"alternatives": ["condition"]},
        ],
        {
            "long": _verified("long-overlap", 0, 100),
            "substitute": _verified("substitute", 200, 220),
            "condition": _verified("condition", 300, 310),
        },
    )
    sources = {
        "left": [_source(0, 60)],
        "right": [_source(60, 100)],
        "substitute": [_source(200, 220)],
        "condition": [_source(300, 310)],
    }

    union = score_ranking(
        ["left", "right", "condition"],
        mapping.evaluable_groups,
        item_source_spans=sources,
    ).metrics
    via_or = score_ranking(
        ["substitute", "condition"],
        mapping.evaluable_groups,
        item_source_spans=sources,
    ).metrics

    assert union["all_required_groups_success_at_5"] == 1.0
    assert union["coverage_ndcg_at_10"] < 1.0
    assert via_or["all_required_groups_success_at_5"] == 1.0


def test_strict_scoring_blocks_missing_source_provenance():
    mapping = _mapping(
        "q-provenance",
        "p1",
        "biology",
        [{"alternatives": ["a"]}],
        {"a": _verified("chunk-a", 0, 5)},
    )

    with pytest.raises(EvidenceContractError, match="item_source_spans"):
        score_ranking(["chunk-a"], mapping.evaluable_groups)


def test_gold_span_quote_length_must_match_locator():
    with pytest.raises(EvidenceContractError, match="length"):
        GoldEvidenceSpan(
            file_id="file-1",
            file_hash="a" * 64,
            pdf_page_index=0,
            char_start_in_normalized_page=0,
            char_end_in_normalized_page=10,
            page_text_hash="b" * 64,
            evidence_text="short",
            evidence_text_hash=hashlib.sha256(b"short").hexdigest(),
        )


def test_mapping_coverage_gate_is_group_weighted_and_per_paper():
    mapped = _mapping(
        "q1",
        "p1",
        "biology",
        [{"alternatives": ["a"]}],
        {"a": _verified("chunk-a", 0, 5)},
    )
    unmapped = _mapping(
        "q2",
        "p2",
        "economics",
        [{"alternatives": ["b"]}],
        {},
    )

    coverage = evaluate_mapping_coverage(
        [mapped, unmapped],
        overall_minimum=0.5,
        per_paper_minimum=0.0,
    )
    assert coverage.overall == 0.5
    assert coverage.per_paper == {"p1": 1.0, "p2": 0.0}
    assert coverage.passed

    with pytest.raises(EvidenceCoverageError, match="paper p2"):
        enforce_mapping_coverage(
            [mapped, unmapped],
            overall_minimum=0.5,
            per_paper_minimum=0.9,
        )


def test_macro_aggregation_is_question_then_paper_then_domain():
    scores = [
        QuestionScore("q1", "p1", "d1", "lookup", {"primary": 1.0}),
        QuestionScore("q2", "p1", "d1", "lookup", {"primary": 0.0}),
        QuestionScore("q3", "p2", "d1", "lookup", {"primary": 1.0}),
        QuestionScore("q4", "p3", "d2", "lookup", {"primary": 0.0}),
    ]

    aggregate = macro_aggregate(scores)

    assert aggregate.by_paper["p1"]["primary"] == 0.5
    assert aggregate.by_domain["d1"]["primary"] == 0.75
    # Domains are equal-weighted: (0.75 + 0.0) / 2, not 2.0 / 4 questions.
    assert aggregate.overall["primary"] == 0.375
    assert aggregate.by_question_type["lookup"]["primary"] == 0.375


def test_paired_bootstrap_is_deterministic_and_domain_stratified():
    candidate = {"p1": 0.9, "p2": 0.7, "p3": 0.4, "p4": 0.6}
    baseline = {"p1": 0.8, "p2": 0.5, "p3": 0.5, "p4": 0.5}
    domains = {"p1": "d1", "p2": "d1", "p3": "d2", "p4": "d2"}

    first = paired_bootstrap(
        candidate,
        baseline,
        domains,
        samples=500,
        seed="fixed",
    )
    second = paired_bootstrap(
        candidate,
        baseline,
        domains,
        samples=500,
        seed="fixed",
    )

    assert first == second
    assert first.observed_delta == pytest.approx(0.075)
    assert first.lower <= first.observed_delta <= first.upper


def test_practical_tie_uses_latency_then_size_chunk_count_and_id():
    ranked = rank_candidates(
        [
            CandidateSummary(
                "slow",
                0.801,
                20,
                100,
                10,
                latency_decisive=True,
            ),
            CandidateSummary(
                "fast",
                0.800,
                10,
                200,
                20,
                latency_decisive=True,
            ),
            CandidateSummary(
                "outside",
                0.790,
                1,
                1,
                1,
                latency_decisive=True,
            ),
            CandidateSummary(
                "incomplete", 1.0, 0, 0, 0, complete=False
            ),
        ]
    )

    assert [item.config_id for item in ranked] == ["fast", "slow", "outside"]


def test_observed_only_latency_cannot_decide_a_quality_tie():
    ranked = rank_candidates(
        [
            CandidateSummary("slow-small", 0.801, 20, 100, 10),
            CandidateSummary("fast-large", 0.800, 10, 200, 20),
        ]
    )

    assert [item.config_id for item in ranked] == [
        "slow-small",
        "fast-large",
    ]


def test_practical_tie_includes_exact_boundary_and_full_tiebreak_chain():
    ranked = rank_candidates(
        [
            CandidateSummary(
                "z-quality", 0.805, 50, 100, 10, latency_decisive=True
            ),
            CandidateSummary(
                "e-latency", 0.800, 10, 500, 50, latency_decisive=True
            ),
            CandidateSummary(
                "d-size", 0.8005, 10, 400, 50, latency_decisive=True
            ),
            CandidateSummary(
                "c-chunks", 0.801, 10, 400, 40, latency_decisive=True
            ),
            CandidateSummary(
                "b-id", 0.803, 10, 400, 40, latency_decisive=True
            ),
            CandidateSummary(
                "a-id", 0.802, 10, 400, 40, latency_decisive=True
            ),
            CandidateSummary(
                "outside", 0.7999, 1, 1, 1, latency_decisive=True
            ),
        ],
        tie_threshold=0.005,
    )

    assert [item.config_id for item in ranked] == [
        "a-id",
        "b-id",
        "c-chunks",
        "d-size",
        "e-latency",
        "z-quality",
        "outside",
    ]
