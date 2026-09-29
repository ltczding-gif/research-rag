"""Optional cross-encoder reranking of first-stage source-passage candidates.

Providers
  none       reranking unavailable (default)
  fastembed  in-process ONNX cross-encoder (CPU); default model
             jinaai/jina-reranker-v2-base-multilingual
  http       a /v1/rerank endpoint in the Jina/Cohere shape, as served by
             llama.cpp (`llama-server --reranking`), vLLM and hosted APIs;
             e.g. bge-reranker-v2-m3 on a local GPU

The reranker only reorders candidates the first stage already returned. It
never adds passages, so canonical source verification is unchanged.
"""
from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from config import (
    RERANKER,
    RERANK_API_KEY,
    RERANK_MODEL,
    RERANK_REVISION,
    RERANK_URL,
)

PROVIDERS = ("none", "fastembed", "http")
DEFAULT_FASTEMBED_MODEL = "jinaai/jina-reranker-v2-base-multilingual"
# Scores are rounded before sorting so tiny GPU nondeterminism cannot reorder
# near ties; remaining ties keep first-stage order.
SCORE_DECIMALS = 5

_fastembed_singleton = None
_identity_cache = None


class RerankerError(ValueError):
    pass


def configured() -> bool:
    return RERANKER in ("fastembed", "http")


def _model_name() -> str:
    if RERANK_MODEL:
        return RERANK_MODEL
    return DEFAULT_FASTEMBED_MODEL if RERANKER == "fastembed" else ""


def _get_fastembed():
    global _fastembed_singleton
    if _fastembed_singleton is None:
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:  # pragma: no cover - fastembed is a default dependency
            raise RerankerError("fastembed reranking requires `pip install fastembed>=0.4`") from exc
        _fastembed_singleton = TextCrossEncoder(model_name=_model_name())
    return _fastembed_singleton


def _fastembed_revision(model) -> tuple[str, str]:
    directory = getattr(getattr(model, "model", None), "_model_dir", None)
    if directory is None:
        import fastembed

        return f"fastembed-{fastembed.__version__}:{_model_name()}", "package_version"
    hashes = {}
    for path in sorted(Path(directory).rglob("*")):
        relative = path.relative_to(directory)
        if path.is_file() and not any(part.startswith(".") for part in relative.parts):
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(block)
            hashes[relative.as_posix()] = digest.hexdigest()
    return hashlib.sha256(json.dumps(hashes, sort_keys=True).encode()).hexdigest(), "local_artifact_hash"


def _endpoint() -> tuple[str, str | None]:
    """Request URL without credentials, plus any user:password from the URL."""
    parsed = urlsplit(RERANK_URL.rstrip("/"))
    path = parsed.path if parsed.path.endswith("/rerank") else parsed.path + "/v1/rerank"
    userinfo, _, host = parsed.netloc.rpartition("@")
    return urlunsplit((parsed.scheme, host, path, "", "")), (userinfo or None)


def identity() -> dict:
    """Provider, model and revision; recorded with every reranked evaluation run."""
    global _identity_cache
    if not configured():
        return {"provider": "none"}
    if _identity_cache is None:
        if RERANKER == "fastembed":
            revision, kind = _fastembed_revision(_get_fastembed())
            endpoint = "in-process"
        else:
            if not RERANK_URL or not _model_name():
                raise RerankerError("LOCALRAG_RERANKER=http needs LOCALRAG_RERANK_URL and LOCALRAG_RERANK_MODEL")
            revision, kind = (RERANK_REVISION or "undeclared"), "operator_declared"
            endpoint = _endpoint()[0]
        _identity_cache = {"provider": RERANKER, "model": _model_name(), "revision": revision,
                           "revision_kind": kind, "endpoint": endpoint, "query_text": "original_question"}
    return dict(_identity_cache)


def _http_scores(query: str, documents: list[str]) -> list[float]:
    body = json.dumps({"model": _model_name(), "query": query, "documents": documents,
                       "top_n": len(documents)}).encode("utf-8")
    url, userinfo = _endpoint()
    headers = {"Content-Type": "application/json"}
    if RERANK_API_KEY:
        headers["Authorization"] = "Bearer " + RERANK_API_KEY
    elif userinfo:
        headers["Authorization"] = "Basic " + base64.b64encode(userinfo.encode("utf-8")).decode("ascii")
    request = urllib.request.Request(url, data=body, headers=headers)
    with urllib.request.urlopen(request, timeout=float(os.environ.get("LOCALRAG_RERANK_TIMEOUT", "120"))) as response:
        payload = json.loads(response.read())
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        raise RerankerError("Rerank endpoint returned no results list")
    scores: list[float | None] = [None] * len(documents)
    for item in results:
        index = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        if not isinstance(index, int) or not 0 <= index < len(documents) or scores[index] is not None:
            raise RerankerError("Rerank endpoint returned an invalid or duplicate index")
        scores[index] = float(score)
    if any(score is None for score in scores):
        raise RerankerError("Rerank endpoint did not score every candidate")
    return scores  # type: ignore[return-value]


def scores(query: str, documents: list[str]) -> list[float]:
    """Relevance score per document, in input order."""
    if not configured():
        raise RerankerError("No reranker configured; set LOCALRAG_RERANKER to fastembed or http")
    if not documents:
        return []
    if RERANKER == "fastembed":
        values = [float(value) for value in _get_fastembed().rerank(query, documents)]
    else:
        values = _http_scores(query, documents)
    if len(values) != len(documents) or not all(math.isfinite(value) for value in values):
        raise RerankerError("Reranker returned invalid scores")
    return values


def rerank_order(query: str, documents: list[str]) -> list[tuple[int, float]]:
    """(original index, rounded score), best first; ties keep first-stage order."""
    rounded = [round(value, SCORE_DECIMALS) for value in scores(query, documents)]
    return sorted(((index, value) for index, value in enumerate(rounded)), key=lambda item: (-item[1], item[0]))
