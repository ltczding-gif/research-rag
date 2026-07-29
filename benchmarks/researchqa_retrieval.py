"""Deterministic offline retrieval primitives for the ResearchQA sweep."""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Mapping, Protocol, Sequence, runtime_checkable


RRF_K = 60
BM25_K1 = 1.2
BM25_B = 0.75
RERANKER_MODEL_ID = "Qwen/Qwen3-Reranker-0.6B"
RERANKER_REVISION = "e61197ed45024b0ed8a2d74b80b4d909f1255473"
SOURCE_COMPOSITION_IDS = (
    "pdf-only",
    "note-to-pdf",
    "pdf-note-rrf",
    "note-guided-pdf",
    "hierarchical-pdf",
)
_TOKEN_RE = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)


@dataclass(frozen=True)
class RetrievalHit:
    """One stable ranked retrieval result."""

    item_id: str
    score: float
    source: str = ""
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.item_id:
            raise ValueError("item_id must be non-empty")
        if not math.isfinite(self.score):
            raise ValueError("retrieval score must be finite")


def _rank_hits(hits: Sequence[RetrievalHit], top_k: int | None) -> tuple[RetrievalHit, ...]:
    if top_k is not None and top_k < 0:
        raise ValueError("top_k must be non-negative")
    ranked = sorted(hits, key=lambda hit: (-hit.score, hit.item_id))
    return tuple(ranked if top_k is None else ranked[:top_k])


def _require_unique_ids(item_ids: Sequence[str]) -> None:
    if len(set(item_ids)) != len(item_ids):
        raise ValueError("item ids must be unique")


def _as_float_vector(vector: Sequence[float]) -> tuple[float, ...]:
    values = tuple(float(value) for value in vector)
    if not values:
        raise ValueError("embedding vectors must be non-empty")
    if not all(math.isfinite(value) for value in values):
        raise ValueError("embedding vectors must contain only finite values")
    return values


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Return exact cosine similarity using deterministic Python arithmetic."""
    left_values = _as_float_vector(left)
    right_values = _as_float_vector(right)
    if len(left_values) != len(right_values):
        raise ValueError("embedding dimensions do not match")
    left_norm_sq = math.fsum(value * value for value in left_values)
    right_norm_sq = math.fsum(value * value for value in right_values)
    if left_norm_sq == 0.0 or right_norm_sq == 0.0:
        raise ValueError("cosine similarity is undefined for zero vectors")
    dot = math.fsum(
        left_value * right_value
        for left_value, right_value in zip(left_values, right_values)
    )
    return dot / math.sqrt(left_norm_sq * right_norm_sq)


def exact_cosine_search(
    query_embedding: Sequence[float],
    item_embeddings: Mapping[str, Sequence[float]],
    *,
    top_k: int | None = 10,
) -> tuple[RetrievalHit, ...]:
    """Rank an in-memory embedding matrix by exact cosine.

    NumPy is used when available so the full rq-2 sweep executes as one exact
    matrix operation per query. The dependency-free scalar path remains for
    minimal installs and unit fixtures.
    """
    if not item_embeddings:
        return ()
    try:
        import numpy as np
    except ImportError:
        np = None

    if np is None:
        hits = [
            RetrievalHit(
                item_id=item_id,
                score=cosine_similarity(query_embedding, embedding),
                source="dense",
            )
            for item_id, embedding in item_embeddings.items()
        ]
    else:
        item_ids = tuple(sorted(item_embeddings))
        query = np.asarray(query_embedding, dtype=np.float32)
        matrix = np.asarray(
            [item_embeddings[item_id] for item_id in item_ids],
            dtype=np.float32,
        )
        if query.ndim != 1 or matrix.ndim != 2 or matrix.shape[1] != query.shape[0]:
            raise ValueError("embedding dimensions must match")
        if not np.isfinite(query).all() or not np.isfinite(matrix).all():
            raise ValueError("embedding vectors must contain only finite values")
        query_norm = np.linalg.norm(query)
        item_norms = np.linalg.norm(matrix, axis=1)
        if query_norm == 0 or np.any(item_norms == 0):
            raise ValueError("cosine similarity is undefined for zero vectors")
        scores = (matrix @ query) / (item_norms * query_norm)
        hits = [
            RetrievalHit(
                item_id=item_id,
                score=float(score),
                source="dense",
            )
            for item_id, score in zip(item_ids, scores, strict=True)
        ]
    return _rank_hits(hits, top_k)


def text_sha256(text: str) -> str:
    """Hash the exact UTF-8 text submitted to the embedding endpoint."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def embedding_cache_key(
    *,
    model_digest: str,
    normalization_revision: str,
    text: str,
) -> str:
    """Build the per-text cache key required by the RQ2 contract."""
    if not model_digest or not normalization_revision:
        raise ValueError("model_digest and normalization_revision are required")
    return ":".join(
        (
            model_digest,
            normalization_revision,
            text_sha256(text),
        )
    )


def batch_embedding_cache_key(
    *,
    model_digest: str,
    normalization_revision: str,
    texts: Sequence[str],
) -> str:
    """Hash an ordered batch of per-text cache keys for artifact reuse."""
    if not texts:
        raise ValueError("embedding batches must be non-empty")
    digest = hashlib.sha256()
    for key in embedding_cache_keys(
        model_digest=model_digest,
        normalization_revision=normalization_revision,
        texts=texts,
    ):
        encoded = key.encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return f"batch-{digest.hexdigest()}"


def embedding_cache_keys(
    *,
    model_digest: str,
    normalization_revision: str,
    texts: Sequence[str],
) -> tuple[str, ...]:
    """Return one cache key per text while preserving batch order."""
    return tuple(
        embedding_cache_key(
            model_digest=model_digest,
            normalization_revision=normalization_revision,
            text=text,
        )
        for text in texts
    )


def bm25_tokenize(text: str) -> tuple[str, ...]:
    """NFKC/lower tokenization preserving internal hyphens and digits."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    return tuple(match.group(0) for match in _TOKEN_RE.finditer(normalized))


class BM25Index:
    """Small dependency-free BM25 index with fixed RQ2 parameters."""

    def __init__(
        self,
        documents: Mapping[str, str],
        *,
        k1: float = BM25_K1,
        b: float = BM25_B,
    ) -> None:
        if k1 <= 0:
            raise ValueError("k1 must be greater than zero")
        if not 0 <= b <= 1:
            raise ValueError("b must be between zero and one")
        if not documents:
            raise ValueError("BM25 requires at least one document")
        self.k1 = float(k1)
        self.b = float(b)
        self.document_ids = tuple(documents)
        _require_unique_ids(self.document_ids)
        self._term_frequencies: dict[str, dict[str, int]] = {}
        self._document_lengths: dict[str, int] = {}
        document_frequency: dict[str, int] = {}
        for document_id, text in documents.items():
            tokens = bm25_tokenize(text)
            frequencies: dict[str, int] = {}
            for token in tokens:
                frequencies[token] = frequencies.get(token, 0) + 1
            self._term_frequencies[document_id] = frequencies
            self._document_lengths[document_id] = len(tokens)
            for token in frequencies:
                document_frequency[token] = document_frequency.get(token, 0) + 1
        self.average_document_length = (
            math.fsum(self._document_lengths.values()) / len(documents)
        )
        count = len(documents)
        self._idf = {
            term: math.log(1.0 + (count - frequency + 0.5) / (frequency + 0.5))
            for term, frequency in document_frequency.items()
        }

    def score(self, query: str, document_id: str) -> float:
        """Score one document, counting repeated query terms once."""
        if document_id not in self._term_frequencies:
            raise KeyError(document_id)
        frequencies = self._term_frequencies[document_id]
        document_length = self._document_lengths[document_id]
        if self.average_document_length == 0:
            return 0.0
        score = 0.0
        for term in dict.fromkeys(bm25_tokenize(query)):
            frequency = frequencies.get(term, 0)
            if not frequency:
                continue
            denominator = frequency + self.k1 * (
                1.0
                - self.b
                + self.b * document_length / self.average_document_length
            )
            score += self._idf[term] * (
                frequency * (self.k1 + 1.0) / denominator
            )
        return score

    def search(
        self,
        query: str,
        *,
        top_k: int | None = 10,
    ) -> tuple[RetrievalHit, ...]:
        """Rank all documents by BM25 with stable ID tie-breaking."""
        hits = [
            RetrievalHit(
                item_id=document_id,
                score=self.score(query, document_id),
                source="bm25",
            )
            for document_id in self.document_ids
        ]
        return _rank_hits(hits, top_k)


def reciprocal_rank_fusion(
    rankings: Sequence[Sequence[RetrievalHit | str]],
    *,
    k: int = RRF_K,
    weights: Sequence[float] | None = None,
    top_k: int | None = None,
    source: str = "rrf",
) -> tuple[RetrievalHit, ...]:
    """Fuse ranked lists with stable, equal-weight RRF by default."""
    if k < 0:
        raise ValueError("RRF k must be non-negative")
    if not rankings:
        return ()
    if weights is None:
        weights = (1.0,) * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights must align with rankings")
    if not all(math.isfinite(weight) and weight >= 0 for weight in weights):
        raise ValueError("RRF weights must be finite and non-negative")
    scores: dict[str, float] = {}
    metadata: dict[str, Mapping[str, object]] = {}
    for ranking, weight in zip(rankings, weights):
        seen: set[str] = set()
        for rank, raw_hit in enumerate(ranking, 1):
            hit = (
                raw_hit
                if isinstance(raw_hit, RetrievalHit)
                else RetrievalHit(str(raw_hit), 0.0)
            )
            if hit.item_id in seen:
                raise ValueError("a ranking cannot contain duplicate item ids")
            seen.add(hit.item_id)
            scores[hit.item_id] = scores.get(hit.item_id, 0.0) + weight / (k + rank)
            metadata.setdefault(hit.item_id, hit.metadata)
    return _rank_hits(
        [
            RetrievalHit(
                item_id=item_id,
                score=score,
                source=source,
                metadata=metadata[item_id],
            )
            for item_id, score in scores.items()
        ],
        top_k,
    )


def preserve_top1_rank_rrf(
    base_hits: Sequence[RetrievalHit],
    reranked_hits: Sequence[RetrievalHit],
    *,
    depth: int,
    top_k: int,
    k: int = RRF_K,
) -> tuple[RetrievalHit, ...]:
    """Fuse base/reranker ranks while deterministically protecting base top-1."""

    if depth < 1 or top_k < 1:
        raise ValueError("depth and top_k must be positive")
    if not base_hits:
        return ()
    fused = reciprocal_rank_fusion(
        (tuple(base_hits[:depth]), tuple(reranked_hits)),
        k=k,
        top_k=None,
        source="rerank-base-rank-rrf",
    )
    protected_id = base_hits[0].item_id
    fused_by_id = {hit.item_id: hit for hit in fused}
    protected_fused = fused_by_id.get(protected_id)
    remaining = tuple(
        hit for hit in fused if hit.item_id != protected_id
    )
    maximum_score = max(
        (hit.score for hit in fused),
        default=0.0,
    )
    protected_score = math.nextafter(maximum_score, math.inf)
    protected = RetrievalHit(
        item_id=protected_id,
        score=protected_score,
        source="rerank-base-rank-rrf",
        metadata={
            **dict(base_hits[0].metadata),
            "protected_base_top1": True,
            "unprotected_rrf_score": (
                protected_fused.score
                if protected_fused is not None
                else 0.0
            ),
        },
    )
    return (protected, *remaining[: max(0, top_k - 1)])


def preserve_dense_top1_weighted_rrf(
    dense_hits: Sequence[RetrievalHit],
    bm25_hits: Sequence[RetrievalHit],
    *,
    dense_weight: float,
    bm25_weight: float,
    top_k: int,
    k: int = RRF_K,
) -> tuple[RetrievalHit, ...]:
    """Fuse dense/BM25 ranks while deterministically protecting dense top-1."""

    if top_k < 1:
        raise ValueError("top_k must be positive")
    fused = reciprocal_rank_fusion(
        (tuple(dense_hits), tuple(bm25_hits)),
        k=k,
        weights=(dense_weight, bm25_weight),
        top_k=None,
        source="dense-bm25-weighted-rrf",
    )
    if not dense_hits:
        return fused[:top_k]
    protected_id = dense_hits[0].item_id
    fused_by_id = {hit.item_id: hit for hit in fused}
    protected_fused = fused_by_id.get(protected_id)
    remaining = tuple(
        hit for hit in fused if hit.item_id != protected_id
    )
    maximum_score = max(
        (hit.score for hit in fused),
        default=0.0,
    )
    protected = RetrievalHit(
        item_id=protected_id,
        score=math.nextafter(maximum_score, math.inf),
        source="dense-bm25-weighted-rrf",
        metadata={
            **dict(dense_hits[0].metadata),
            "protected_dense_top1": True,
            "dense_weight": dense_weight,
            "bm25_weight": bm25_weight,
            "unprotected_rrf_score": (
                protected_fused.score
                if protected_fused is not None
                else 0.0
            ),
        },
    )
    return (protected, *remaining[: max(0, top_k - 1)])


def pdf_only(
    pdf_hits: Sequence[RetrievalHit],
    *,
    top_k: int | None = None,
) -> tuple[RetrievalHit, ...]:
    """Return direct benchmark-PDF hits with deterministic ordering."""
    return _rank_hits(
        [
            RetrievalHit(
                item_id=hit.item_id,
                score=hit.score,
                source="pdf-only",
                metadata=hit.metadata,
            )
            for hit in pdf_hits
        ],
        top_k,
    )


def note_to_pdf(
    note_hits: Sequence[RetrievalHit],
    note_pdf_backlinks: Mapping[str, Sequence[str]],
    *,
    top_k: int | None = None,
    k: int = RRF_K,
) -> tuple[RetrievalHit, ...]:
    """Project ranked note chunks to cited benchmark-PDF chunks."""
    projected = []
    for note_rank, note_hit in enumerate(note_hits, 1):
        pdf_ids = tuple(dict.fromkeys(note_pdf_backlinks.get(note_hit.item_id, ())))
        for citation_rank, pdf_id in enumerate(pdf_ids, 1):
            projected.append(
                RetrievalHit(
                    item_id=pdf_id,
                    score=1.0 / (k + note_rank) + 1.0 / (k + citation_rank),
                    source="note-to-pdf",
                    metadata={"via_note_id": note_hit.item_id},
                )
            )
    aggregated: dict[str, float] = {}
    via: dict[str, list[str]] = {}
    for hit in projected:
        aggregated[hit.item_id] = aggregated.get(hit.item_id, 0.0) + hit.score
        via.setdefault(hit.item_id, []).append(str(hit.metadata["via_note_id"]))
    return _rank_hits(
        [
            RetrievalHit(
                item_id=item_id,
                score=score,
                source="note-to-pdf",
                metadata={"via_note_ids": tuple(dict.fromkeys(via[item_id]))},
            )
            for item_id, score in aggregated.items()
        ],
        top_k,
    )


def pdf_note_rrf(
    direct_pdf_hits: Sequence[RetrievalHit],
    note_derived_pdf_hits: Sequence[RetrievalHit],
    *,
    top_k: int | None = None,
    k: int = RRF_K,
) -> tuple[RetrievalHit, ...]:
    """Fuse direct PDF and note-derived PDF rankings with equal-weight RRF."""
    return reciprocal_rank_fusion(
        (direct_pdf_hits, note_derived_pdf_hits),
        k=k,
        top_k=top_k,
        source="pdf-note-rrf",
    )


def note_guided_pdf(
    note_hits: Sequence[RetrievalHit],
    pdf_hits: Sequence[RetrievalHit],
    note_pdf_backlinks: Mapping[str, Sequence[str]],
    *,
    top_k: int | None = None,
) -> tuple[RetrievalHit, ...]:
    """Filter direct PDF ranking to the ranges selected by retrieved notes."""
    allowed = {
        pdf_id
        for note_hit in note_hits
        for pdf_id in note_pdf_backlinks.get(note_hit.item_id, ())
    }
    return _rank_hits(
        [
            RetrievalHit(
                item_id=hit.item_id,
                score=hit.score,
                source="note-guided-pdf",
                metadata=hit.metadata,
            )
            for hit in pdf_hits
            if hit.item_id in allowed
        ],
        top_k,
    )


def hierarchical_pdf(
    parent_hits: Sequence[RetrievalHit],
    child_hits_by_parent: Mapping[str, Sequence[RetrievalHit]],
    *,
    top_k: int | None = None,
    k: int = RRF_K,
) -> tuple[RetrievalHit, ...]:
    """Rank child chunks within retrieved parent sections."""
    scores: dict[str, float] = {}
    parent_ids: dict[str, str] = {}
    for parent_rank, parent_hit in enumerate(parent_hits, 1):
        children = child_hits_by_parent.get(parent_hit.item_id, ())
        for child_rank, child_hit in enumerate(children, 1):
            score = 1.0 / (k + parent_rank) + 1.0 / (k + child_rank)
            existing = scores.get(child_hit.item_id)
            if existing is None or score > existing:
                scores[child_hit.item_id] = score
                parent_ids[child_hit.item_id] = parent_hit.item_id
    return _rank_hits(
        [
            RetrievalHit(
                item_id=item_id,
                score=score,
                source="hierarchical-pdf",
                metadata={"parent_id": parent_ids[item_id]},
            )
            for item_id, score in scores.items()
        ],
        top_k,
    )


@runtime_checkable
class RerankerAdapter(Protocol):
    """Adapter contract; implementations own model loading outside this module."""

    model_id: str
    revision: str

    def score_pairs(
        self,
        query: str,
        passages: Sequence[str],
        *,
        batch_size: int,
    ) -> Sequence[float]:
        """Return one relevance score for each input passage."""


@dataclass(frozen=True)
class RerankResult:
    """Reranked hits plus explicit candidate/truncation accounting."""

    hits: tuple[RetrievalHit, ...]
    original_length: int
    candidate_length: int
    retained_length: int
    truncated: bool
    truncation_direction: str | None


def rerank_hits(
    query: str,
    hits: Sequence[RetrievalHit],
    passages: Mapping[str, str],
    adapter: RerankerAdapter,
    *,
    depth: int,
    keep: int = 10,
    batch_size: int = 8,
) -> RerankResult:
    """Rerank the first ``depth`` hits and retain ``keep`` stable results."""
    if depth <= 0 or keep <= 0 or batch_size <= 0:
        raise ValueError("depth, keep, and batch_size must be greater than zero")
    if adapter.model_id != RERANKER_MODEL_ID:
        raise ValueError(f"unexpected reranker model: {adapter.model_id}")
    if adapter.revision != RERANKER_REVISION:
        raise ValueError(f"unexpected reranker revision: {adapter.revision}")
    original = tuple(hits)
    candidates = original[:depth]
    try:
        candidate_passages = [passages[hit.item_id] for hit in candidates]
    except KeyError as exc:
        raise ValueError(f"missing passage for {exc.args[0]}") from exc
    scores = tuple(
        float(score)
        for score in adapter.score_pairs(
            query,
            candidate_passages,
            batch_size=batch_size,
        )
    )
    if len(scores) != len(candidates):
        raise ValueError("reranker returned the wrong number of scores")
    if not all(math.isfinite(score) for score in scores):
        raise ValueError("reranker returned a non-finite score")
    reranked = _rank_hits(
        [
            RetrievalHit(
                item_id=hit.item_id,
                score=score,
                source="reranker",
                metadata={
                    **dict(hit.metadata),
                    "pre_rerank_score": hit.score,
                },
            )
            for hit, score in zip(candidates, scores)
        ],
        keep,
    )
    truncated = len(original) > len(candidates) or len(candidates) > len(reranked)
    return RerankResult(
        hits=reranked,
        original_length=len(original),
        candidate_length=len(candidates),
        retained_length=len(reranked),
        truncated=truncated,
        truncation_direction="tail" if truncated else None,
    )


__all__ = [
    "BM25Index",
    "BM25_B",
    "BM25_K1",
    "RERANKER_MODEL_ID",
    "RERANKER_REVISION",
    "RRF_K",
    "RerankResult",
    "RerankerAdapter",
    "RetrievalHit",
    "SOURCE_COMPOSITION_IDS",
    "batch_embedding_cache_key",
    "bm25_tokenize",
    "cosine_similarity",
    "embedding_cache_key",
    "embedding_cache_keys",
    "exact_cosine_search",
    "hierarchical_pdf",
    "note_guided_pdf",
    "note_to_pdf",
    "pdf_note_rrf",
    "pdf_only",
    "preserve_dense_top1_weighted_rrf",
    "preserve_top1_rank_rrf",
    "reciprocal_rank_fusion",
    "rerank_hits",
    "text_sha256",
]
