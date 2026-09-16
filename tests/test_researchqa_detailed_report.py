from __future__ import annotations

from benchmarks.researchqa_detailed_report import _question_analysis


def _payload(config_id: str, *, relevant_rank: int) -> dict[str, object]:
    ranked = [f"chunk-{index}" for index in range(20)]
    relevant_id = ranked[relevant_rank - 1]
    return {
        "candidate": {
            "config_id": config_id,
            "retriever": "dense",
            "source_composition": "pdf-only",
            "reranker": "rerank-off",
        },
        "question_results": [
            {
                "row_id": "W1_adversarial0",
                "domain": "mathematics",
                "question_type": "adversarial",
                "ranked_item_ids": ranked,
                "pre_rerank_item_ids": [],
                "metrics": {
                    "recall_at_10": 0.0,
                    "coverage_ndcg_at_10": 0.0,
                },
            }
        ],
        "mapping": {
            "mappings": [
                {
                    "row_id": "W1_adversarial0",
                    "groups": [
                        {
                            "alternatives": [
                                {"mapped_item_ids": [relevant_id]}
                            ]
                        }
                    ],
                }
            ]
        },
    }


def test_question_analysis_finds_shared_candidate_generation_failure():
    result = _question_analysis(
        {
            "candidate-a": _payload("candidate-a", relevant_rank=13),
            "candidate-b": _payload("candidate-b", relevant_rank=18),
        }
    )

    assert result["candidate_count"] == 2
    assert result["evaluable_count"] == 1
    assert result["never_perfect_count"] == 1
    assert result["failure_rate_by_type"] == {"adversarial": 1.0}
    assert result["failure_rate_by_domain"] == {"mathematics": 1.0}
    assert result["all_failed"] == [
        {
            "row_id": "W1_adversarial0",
            "domain": "mathematics",
            "question_type": "adversarial",
            "misses": 2,
            "successes": 0,
            "best_ndcg": 0.0,
            "best_relevant_rank": 13,
            "best_routes": ["dense/pdf-only/rerank-off"],
            "best_pre_rerank_rank": None,
        }
    ]
