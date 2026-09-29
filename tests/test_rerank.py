"""Cross-encoder reranking: opt-in, reorder-only, pinned pools, providers."""
from __future__ import annotations

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_retrieval_eval import _span, _write_suite, core  # noqa: E402,F401  (fixture re-export)

from benchmarks import retrieval_eval as ev  # noqa: E402


def _keyword_scorer(query, documents):
    """Fake cross-encoder: scores documents by shared lowercase words."""
    words = set(query.lower().split())
    return [float(len(words & set(doc.lower().replace(",", " ").split()))) for doc in documents]


@pytest.fixture
def reranking(core, monkeypatch):  # noqa: F811
    seen = []

    def scores(query, documents):
        seen.append(list(documents))
        return _keyword_scorer(query, documents)

    monkeypatch.setattr(core.reranker, "RERANKER", "fastembed")
    monkeypatch.setattr(core.reranker, "scores", scores)
    monkeypatch.setattr(core.reranker, "identity", lambda: {
        "provider": "fastembed", "model": "fake-cross-encoder", "revision": "r1",
        "revision_kind": "test", "endpoint": "in-process", "query_text": "original_question"})
    return core, seen


def test_reranking_is_off_by_default_and_needs_a_configured_reranker(core):  # noqa: F811
    payload, status = core.search_papers_chroma("cobalt", n=2)
    assert status == 200 and payload["reranked"] is False
    payload, status = core.search_papers_chroma("cobalt", n=2, rerank=True)
    assert status == 409 and "LOCALRAG_RERANKER" in payload["error"]
    assert core.search_papers_chroma("cobalt", rerank="yes")[1] == 400


def test_rerank_reorders_the_first_stage_pool_and_never_adds_hits(reranking, monkeypatch):
    core, seen = reranking
    monkeypatch.setattr(core, "RERANK_CANDIDATES", 3)
    dense, _ = core.search_papers_chroma("cobalt 240 mV overpotential nickel", n=3)
    payload, status = core.search_papers_chroma("cobalt 240 mV overpotential nickel", n=2, rerank=True)
    assert status == 200 and payload["reranked"] is True
    assert len(seen[-1]) == 3  # exactly the pinned pool, not n
    assert {hit["id"] for hit in payload["results"]} <= {hit["id"] for hit in dense["results"]}
    top = payload["results"][0]
    assert top["id"] == "chunk-ATTB-0"
    assert top["retrieval"]["reranked"] is True and top["retrieval"]["stage1_rank"] >= 1
    assert all(hit["evidence"]["verified"] for hit in payload["results"])


def test_equal_scores_keep_first_stage_order(reranking, monkeypatch):
    core, _ = reranking
    monkeypatch.setattr(core.reranker, "scores", lambda q, docs: [0.5 + 1e-9 * i for i in range(len(docs))])
    dense, _ = core.search_papers_chroma("cobalt", n=4)
    reranked, _ = core.search_papers_chroma("cobalt", n=4, rerank=True)
    # Differences below the rounding precision are ties, so dense order stands.
    assert [h["id"] for h in reranked["results"]] == [h["id"] for h in dense["results"]]


def test_prepare_answer_uses_the_reranked_ranking(reranking):
    core, seen = reranking
    packet, status = core.prepare_answer_payload("nickel 240 mV", n=1, rerank=True)
    assert status == 200 and seen
    assert packet["evidence"][0]["metadata"]["zotero_attachment_key"] == "ATTB"


def test_rerank_strategies_pin_rerank_and_record_identity_without_endpoint(reranking, tmp_path, monkeypatch):
    core, _ = reranking
    monkeypatch.setattr(core, "RERANK_DEFAULT", True)
    suite = ev.load_suite(_write_suite(tmp_path / "s.jsonl", [
        {"query_id": "q1", "text": "nickel 240 mV overpotential",
         "evidence": [_span(core, "ATTB", 0, "240 mV at 10 mA cm-2", "e1")]}]))
    dense = ev.run_strategy(core, suite, "dense", ks=(1,), packet_budget=None)
    reranked = ev.run_strategy(core, suite, "dense-rerank", ks=(1,), packet_budget=8000, diagnostic_depth=4)
    assert dense["strategy"]["params"]["rerank"] is False and dense["strategy"]["reranker"] is None
    assert reranked["strategy"]["reranker"]["model"] == "fake-cross-encoder"
    assert "endpoint" not in reranked["strategy"]["reranker"]
    assert reranked["metrics"]["overall"]["span_coverage@1"] == 1.0
    assert reranked["per_query"]["q1"]["span_first_covered_ranks"] == [1]


def test_hybrid_rerank_diagnostics_extend_the_scored_ranking(reranking, tmp_path, monkeypatch):
    core, _ = reranking
    import lexical_index

    if not lexical_index.fts5_available():
        pytest.skip("sqlite3 without FTS5")
    lexical_index.build_sidecar(core.pdf_col, core.pdf_generation)
    monkeypatch.setattr(core, "pdf_lexical", lexical_index.LexicalIndex.open_for(core.pdf_generation))
    monkeypatch.setattr(core, "RERANK_CANDIDATES", 3)
    suite = ev.load_suite(_write_suite(tmp_path / "s.jsonl", [
        {"query_id": "q1", "text": "cobalt loading",
         "evidence": [_span(core, "ATTS", 0, "Co loading of 1.2 wt%", "e1")]}]))
    record = ev.run_strategy(core, suite, "hybrid-rerank", ks=(1, 2), packet_budget=None, diagnostic_depth=5)
    assert record["settings"]["diagnostic_depth"] == 5  # prefix check passed inside run_strategy
    core.pdf_lexical.close()


# -- HTTP provider (llama.cpp / vLLM / Jina-style /v1/rerank) ---------------------


class _RerankHandler(BaseHTTPRequestHandler):
    response = None
    requests = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        type(self).requests.append((self.path, dict(self.headers), body))
        payload = type(self).response(body)
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def http_reranker(monkeypatch):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
    import reranker

    server = HTTPServer(("127.0.0.1", 0), _RerankHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _RerankHandler.requests = []
    monkeypatch.setattr(reranker, "RERANKER", "http")
    monkeypatch.setattr(reranker, "RERANK_URL", f"http://user:secret@127.0.0.1:{server.server_port}")
    monkeypatch.setattr(reranker, "RERANK_MODEL", "bge-reranker-v2-m3")
    monkeypatch.setattr(reranker, "RERANK_API_KEY", "k-123")
    monkeypatch.setattr(reranker, "RERANK_REVISION", "gguf-sha256")
    monkeypatch.setattr(reranker, "_identity_cache", None)
    yield reranker
    server.shutdown()


def test_http_reranker_maps_scores_by_index(http_reranker):
    # The server answers best-first, as llama.cpp does; scores map back by index.
    _RerankHandler.response = staticmethod(lambda body: {"results": [
        {"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.4},
        {"index": 1, "relevance_score": 0.1}]})
    assert http_reranker.scores("q", ["a", "b", "c"]) == [0.4, 0.1, 0.9]
    assert http_reranker.rerank_order("q", ["a", "b", "c"]) == [(2, 0.9), (0, 0.4), (1, 0.1)]
    path, headers, body = _RerankHandler.requests[0]
    assert path == "/v1/rerank" and headers["Authorization"] == "Bearer k-123"
    assert body == {"model": "bge-reranker-v2-m3", "query": "q", "documents": ["a", "b", "c"], "top_n": 3}
    identity = http_reranker.identity()
    assert identity["revision"] == "gguf-sha256" and "secret" not in identity["endpoint"]


@pytest.mark.parametrize("results", [
    [{"index": 0, "relevance_score": 1.0}, {"index": 0, "relevance_score": 0.5}],
    [{"index": 0, "relevance_score": 1.0}],
    [{"index": 5, "relevance_score": 1.0}, {"index": 1, "relevance_score": 0.5}],
    [{"index": 0, "relevance_score": float("nan")}, {"index": 1, "relevance_score": 0.5}],
])
def test_http_reranker_rejects_incomplete_or_invalid_answers(http_reranker, results):
    _RerankHandler.response = staticmethod(lambda body: {"results": results})
    with pytest.raises(http_reranker.RerankerError):
        http_reranker.scores("q", ["a", "b"])


def test_http_reranker_moves_url_credentials_into_basic_auth(http_reranker, monkeypatch):
    monkeypatch.setattr(http_reranker, "RERANK_API_KEY", "")
    _RerankHandler.response = staticmethod(lambda body: {"results": [{"index": 0, "relevance_score": 1.0}]})
    assert http_reranker.scores("q", ["a"]) == [1.0]
    _, headers, _ = _RerankHandler.requests[-1]
    assert headers["Authorization"] == "Basic dXNlcjpzZWNyZXQ="  # user:secret
