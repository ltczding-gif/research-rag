from __future__ import annotations

import math

import pytest

from benchmarks.researchqa_scoring import (
    CandidateSummary,
    EvidenceCoverageError,
    QuestionScore,
    enforce_mapping_coverage,
    evaluate_mapping_coverage,
    macro_aggregate,
    map_reference_groups,
    paired_bootstrap,
    rank_candidates,
    score_ranking,
)


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
            "alpha paraphrase": {
                "mapped_item_ids": ["chunk-a"],
                "match_method": "fuzzy-v1",
                "match_score": 0.95,
            },
            "beta": ["chunk-b", "chunk-b-extra"],
        },
    )

    assert mapping.total_groups == 2
    assert mapping.mapped_groups == 2
    assert mapping.groups[0].mapped
    assert not mapping.groups[0].alternatives[0].mapped
    assert mapping.groups[0].alternatives[1].mapped

    one_group = score_ranking(["chunk-a", "noise"], mapping.groups)
    assert one_group.metrics["recall_at_5"] == 0.5
    assert one_group.metrics["all_required_groups_success_at_5"] == 0.0

    all_groups = score_ranking(["chunk-a", "chunk-b"], mapping.groups)
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
        {"a": "chunk-a", "b": "chunk-b"},
    )
    metrics = score_ranking(
        ["noise", "chunk-a", "noise-2", "chunk-b"],
        mapping.groups,
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


def test_mapping_coverage_gate_is_group_weighted_and_per_paper():
    mapped = _mapping(
        "q1",
        "p1",
        "biology",
        [{"alternatives": ["a"]}],
        {"a": "chunk-a"},
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
            CandidateSummary("slow", 0.801, 20, 100, 10),
            CandidateSummary("fast", 0.800, 10, 200, 20),
            CandidateSummary("outside", 0.790, 1, 1, 1),
            CandidateSummary(
                "incomplete", 1.0, 0, 0, 0, complete=False
            ),
        ]
    )

    assert [item.config_id for item in ranked] == ["fast", "slow", "outside"]


def test_practical_tie_includes_exact_boundary_and_full_tiebreak_chain():
    ranked = rank_candidates(
        [
            CandidateSummary("z-quality", 0.805, 50, 100, 10),
            CandidateSummary("e-latency", 0.800, 10, 500, 50),
            CandidateSummary("d-size", 0.8005, 10, 400, 50),
            CandidateSummary("c-chunks", 0.801, 10, 400, 40),
            CandidateSummary("b-id", 0.803, 10, 400, 40),
            CandidateSummary("a-id", 0.802, 10, 400, 40),
            CandidateSummary("outside", 0.7999, 1, 1, 1),
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
