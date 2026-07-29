from __future__ import annotations

import hashlib
import math

import pytest

from benchmarks.researchqa_retrieval import (
    BM25Index,
    BM25_B,
    BM25_K1,
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
    RetrievalHit,
    batch_embedding_cache_key,
    bm25_tokenize,
    cosine_similarity,
    embedding_cache_key,
    embedding_cache_keys,
    exact_cosine_search,
    hierarchical_pdf,
    note_guided_pdf,
    note_to_pdf,
    pdf_note_rrf,
    pdf_only,
    preserve_top1_rank_rrf,
    reciprocal_rank_fusion,
    rerank_hits,
)


def _hit(item_id: str, score: float, source: str = "") -> RetrievalHit:
    return RetrievalHit(item_id=item_id, score=score, source=source)


def test_exact_cosine_is_not_approximate_and_ties_break_by_id():
    embeddings = {
        "z": (1.0, 0.0),
        "b": (1.0, 1.0),
        "a": (1.0, 1.0),
        "n": (-1.0, 0.0),
    }

    hits = exact_cosine_search((1.0, 1.0), embeddings, top_k=None)

    assert [hit.item_id for hit in hits] == ["a", "b", "z", "n"]
    assert hits[0].score == pytest.approx(1.0)
    assert hits[2].score == pytest.approx(1 / math.sqrt(2))
    assert cosine_similarity((1.0, 2.0), (2.0, 4.0)) == pytest.approx(1.0)


def test_exact_cosine_rejects_zero_and_mismatched_vectors():
    with pytest.raises(ValueError, match="zero vectors"):
        cosine_similarity((0.0, 0.0), (1.0, 0.0))
    with pytest.raises(ValueError, match="dimensions"):
        cosine_similarity((1.0,), (1.0, 2.0))


def test_embedding_cache_keys_bind_model_normalization_and_exact_text():
    model = "d" * 64
    revision = "nfkc-lower-v1"
    expected = f"{model}:{revision}:{hashlib.sha256(b'alpha').hexdigest()}"

    assert embedding_cache_key(
        model_digest=model,
        normalization_revision=revision,
        text="alpha",
    ) == expected
    assert embedding_cache_keys(
        model_digest=model,
        normalization_revision=revision,
        texts=("alpha", "beta"),
    )[0] == expected
    assert batch_embedding_cache_key(
        model_digest=model,
        normalization_revision=revision,
        texts=("alpha", "beta"),
    ) != batch_embedding_cache_key(
        model_digest=model,
        normalization_revision=revision,
        texts=("beta", "alpha"),
    )


def test_bm25_tokenization_is_nfkc_lower_and_preserves_internal_hyphens():
    assert bm25_tokenize("Ｆoo-BAR 123 X_ignored") == (
        "foo-bar",
        "123",
        "x",
        "ignored",
    )


def test_repo_local_bm25_uses_fixed_parameters_and_stable_ranking():
    index = BM25Index(
        {
            "long": "alpha beta beta gamma",
            "short": "alpha beta",
            "none": "delta",
        }
    )

    hits = index.search("beta", top_k=None)

    assert index.k1 == BM25_K1 == 1.2
    assert index.b == BM25_B == 0.75
    assert [hit.item_id for hit in hits] == ["long", "short", "none"]
    assert hits[0].score > hits[1].score > hits[2].score
    assert all(hit.source == "bm25" for hit in hits)


def test_rrf_uses_k60_equal_weights_and_stable_ties():
    first = (_hit("a", 99), _hit("b", 1))
    second = (_hit("b", 99), _hit("a", 1))

    fused = reciprocal_rank_fusion((first, second))

    assert [hit.item_id for hit in fused] == ["a", "b"]
    assert fused[0].score == pytest.approx(1 / 61 + 1 / 62)
    with pytest.raises(ValueError, match="duplicate"):
        reciprocal_rank_fusion((("a", "a"),))


def test_rr1_rank_fusion_protects_base_top1_and_keeps_rrf_order():
    base = tuple(
        _hit(item_id, score)
        for item_id, score in (
            ("protected", 1.0),
            ("b", 0.9),
            ("c", 0.8),
            ("d", 0.7),
        )
    )
    reranked = (
        _hit("d", 4.0),
        _hit("c", 3.0),
        _hit("b", 2.0),
        _hit("protected", 1.0),
    )

    fused = preserve_top1_rank_rrf(
        base,
        reranked,
        depth=4,
        top_k=4,
    )
    ordinary = reciprocal_rank_fusion(
        (base, reranked),
        top_k=None,
    )

    assert fused[0].item_id == "protected"
    assert fused[0].metadata["protected_base_top1"] is True
    assert [hit.item_id for hit in fused[1:]] == [
        hit.item_id
        for hit in ordinary
        if hit.item_id != "protected"
    ]
    assert len({hit.item_id for hit in fused}) == len(fused)
    assert all(
        left.score >= right.score
        for left, right in zip(fused, fused[1:])
    )


def test_pdf_only_and_note_to_pdf_compositions():
    direct = (_hit("p2", 0.8), _hit("p1", 0.9))
    notes = (_hit("n1", 1.0), _hit("n2", 0.5))

    direct_result = pdf_only(direct)
    projected = note_to_pdf(
        notes,
        {"n1": ("p2", "p1"), "n2": ("p1",)},
    )

    assert [hit.item_id for hit in direct_result] == ["p1", "p2"]
    assert all(hit.source == "pdf-only" for hit in direct_result)
    assert [hit.item_id for hit in projected] == ["p1", "p2"]
    assert projected[0].metadata["via_note_ids"] == ("n1", "n2")


def test_pdf_note_rrf_and_note_guided_pdf_compositions():
    direct = (_hit("p1", 0.9), _hit("p2", 0.8), _hit("p3", 0.7))
    derived = (_hit("p2", 1.0), _hit("p3", 0.5))

    fused = pdf_note_rrf(direct, derived)
    guided = note_guided_pdf(
        (_hit("n1", 1.0),),
        direct,
        {"n1": ("p3", "p1")},
    )

    assert fused[0].item_id == "p2"
    assert all(hit.source == "pdf-note-rrf" for hit in fused)
    assert [hit.item_id for hit in guided] == ["p1", "p3"]
    assert all(hit.source == "note-guided-pdf" for hit in guided)


def test_hierarchical_pdf_returns_children_of_ranked_parents():
    parent_hits = (_hit("parent-b", 1.0), _hit("parent-a", 0.9))
    children = {
        "parent-b": (_hit("child-2", 0.9), _hit("child-1", 0.8)),
        "parent-a": (_hit("child-3", 1.0),),
    }

    result = hierarchical_pdf(parent_hits, children)

    assert [hit.item_id for hit in result] == [
        "child-2",
        "child-1",
        "child-3",
    ]
    assert result[0].metadata["parent_id"] == "parent-b"
    assert all(hit.source == "hierarchical-pdf" for hit in result)


class _FakeReranker:
    model_id = RERANKER_MODEL_ID
    revision = RERANKER_REVISION

    def __init__(self):
        self.calls = []

    def score_pairs(self, query, passages, *, batch_size):
        self.calls.append((query, tuple(passages), batch_size))
        return [0.5, 0.9, 0.5][: len(passages)]


def test_reranker_adapter_records_tail_truncation_and_stable_ties():
    adapter = _FakeReranker()
    hits = tuple(_hit(item_id, score) for item_id, score in (
        ("z", 4.0),
        ("b", 3.0),
        ("a", 2.0),
        ("tail", 1.0),
    ))
    passages = {hit.item_id: f"text {hit.item_id}" for hit in hits}

    result = rerank_hits(
        "question",
        hits,
        passages,
        adapter,
        depth=3,
        keep=3,
        batch_size=2,
    )

    assert [hit.item_id for hit in result.hits] == ["b", "a", "z"]
    assert result.original_length == 4
    assert result.candidate_length == 3
    assert result.retained_length == 3
    assert result.truncated
    assert result.truncation_direction == "tail"
    assert adapter.calls == [
        ("question", ("text z", "text b", "text a"), 2)
    ]
    assert result.hits[0].metadata["pre_rerank_score"] == 3.0


def test_reranker_rejects_wrong_pinned_revision_without_model_download():
    class WrongRevision(_FakeReranker):
        revision = "moving-main"

    with pytest.raises(ValueError, match="revision"):
        rerank_hits(
            "q",
            (_hit("a", 1.0),),
            {"a": "passage"},
            WrongRevision(),
            depth=1,
        )
