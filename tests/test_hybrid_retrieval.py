"""Keyword sidecar, hybrid fusion and retrieval-mode contracts."""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_retrieval_eval import _span, _write_suite, core  # noqa: E402,F401  (fixture re-export)

from benchmarks import retrieval_eval as ev  # noqa: E402


@pytest.fixture
def lexical(core):  # noqa: F811 - pytest fixture chaining
    import lexical_index

    if not lexical_index.fts5_available():
        pytest.skip("sqlite3 without FTS5")
    lexical_index.build_sidecar(core.pdf_col, core.pdf_generation)
    index = lexical_index.LexicalIndex.open_for(core.pdf_generation)
    core.pdf_lexical = index
    yield core, lexical_index, index
    index.close()
    core.pdf_lexical = None


def test_query_terms_fold_case_drop_stopwords_and_keep_formula_tokens():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
    import lexical_index

    assert lexical_index.query_terms("What is the OER overpotential of NiFe-LDH at 10 mA cm-2?") == [
        "oer", "overpotential", "nife", "ldh", "10", "ma", "cm", "2"]
    assert lexical_index.query_terms("Pt3Ni 与 Pt3Ni") == ["pt3ni", "与"]


def test_reciprocal_rank_fusion_rewards_agreement_and_breaks_ties_dense_first():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "service"))
    from lexical_index import reciprocal_rank_fusion

    fused = reciprocal_rank_fusion({"dense": ["a", "b", "c"], "lexical": ["c", "d"]})
    # b (dense #2) and d (lexical #2) tie at 1/62; the dense list wins ties.
    assert [identifier for identifier, _ in fused] == ["c", "a", "b", "d"]
    assert fused[0][1] == pytest.approx({"score": 1 / 63 + 1 / 61, "dense_rank": 3, "lexical_rank": 1})


def test_sidecar_is_bound_to_its_generation(lexical, tmp_path):
    core, lexical_index, index = lexical
    assert index.status()["item_count"] == core.pdf_generation.manifest["item_count"]
    hits = index.search("240 mV overpotential", 5)
    assert [chunk_id for chunk_id, _ in hits] == ["chunk-ATTB-0"]
    assert index.search("cobalt", 5, zotero_parent_key="PARENTB") == []
    other = {**core.pdf_generation.manifest, "generation_id": "0" * 32}
    with pytest.raises(lexical_index.LexicalIndexError, match="generation_id"):
        lexical_index.LexicalIndex(lexical_index.sidecar_path(core.pdf_generation), other)
    garbage = tmp_path / "broken.sqlite"
    garbage.write_bytes(b"not a database")
    with pytest.raises(lexical_index.LexicalIndexError, match="unreadable"):
        lexical_index.LexicalIndex(garbage, core.pdf_generation.manifest)


def test_default_mode_stays_dense_even_when_a_keyword_index_exists(lexical):
    core, _, _ = lexical
    payload, status = core.search_papers_chroma("240 mV overpotential", n=2)
    assert status == 200 and payload["retrieval_mode"] == "dense"
    assert all(hit["retrieval"]["mode"] == "dense" for hit in payload["results"])
    auto, status = core.search_papers_chroma("240 mV overpotential", n=2, retrieval_mode="auto")
    assert status == 200 and auto["retrieval_mode"] == "hybrid"
    assert core.index_state()["papers"]["keyword_index"]["ready"] is True


def test_hybrid_promotes_exact_keyword_matches_and_verifies_every_hit(lexical):
    core, _, _ = lexical
    # The bag-of-words embedding cannot see "240 mV"; keyword retrieval can.
    payload, status = core.search_papers_chroma("240 mV overpotential", n=3, retrieval_mode="hybrid")
    assert status == 200
    top = payload["results"][0]
    assert top["id"] == "chunk-ATTB-0"
    assert top["retrieval"]["lexical_rank"] == 1 and "dense_rank" in top["retrieval"]
    assert all(hit["evidence"]["verified"] for hit in payload["results"])


def test_lexical_mode_fetches_text_from_chroma_and_honours_filters(lexical):
    core, _, _ = lexical
    payload, status = core.search_papers_chroma("loading", n=5, retrieval_mode="lexical",
                                                zotero_parent_key="PARENTA", source_role="si")
    assert status == 200
    assert [hit["id"] for hit in payload["results"]] == ["chunk-ATTS-0"]
    assert payload["results"][0]["evidence"]["verified"] is True
    assert payload["results"][0]["distance"] is None


def test_a_damaged_keyword_index_cannot_inject_or_escape_filters(lexical, monkeypatch):
    core, _, index = lexical
    monkeypatch.setattr(index, "search", lambda *a, **k: [("forged-id", 0.0)])
    _, status = core.search_papers_chroma("cobalt", n=2, retrieval_mode="lexical")
    assert status == 409
    monkeypatch.setattr(index, "search", lambda *a, **k: [("chunk-ATTB-0", 0.0)])
    payload, status = core.search_papers_chroma("cobalt", n=2, retrieval_mode="lexical",
                                                zotero_parent_key="PARENTA")
    assert status == 409 and "outside the requested filters" in payload["error"]


def test_hybrid_without_keyword_index_fails_instead_of_falling_back(core):  # noqa: F811
    payload, status = core.search_papers_chroma("cobalt", retrieval_mode="hybrid")
    assert status == 409 and "build_lexical_index" in payload["error"]
    assert core.search_papers_chroma("cobalt", retrieval_mode="auto")[0]["retrieval_mode"] == "dense"
    assert core.search_papers_chroma("cobalt", retrieval_mode="bm25")[1] == 400
    assert core.search_papers_chroma("cobalt", retrieval_mode=3)[1] == 400


def test_build_script_writes_the_sidecar_for_the_active_generation(core):  # noqa: F811
    import chromadb
    import lexical_index

    if not lexical_index.fts5_available():
        pytest.skip("sqlite3 without FTS5")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import build_lexical_index

    store = core.pdf_generation.store
    chroma_path = store.root.parent.parent
    client = chromadb.PersistentClient(path=str(chroma_path))
    result = build_lexical_index.build(chroma_path, "papers", client=client)
    assert result["generation_id"] == core.pdf_generation.manifest["generation_id"]
    with sqlite3.connect(result["path"]) as connection:
        assert connection.execute("SELECT count(*) FROM chunks").fetchone()[0] == result["item_count"]


def test_eval_strategies_pin_their_mode_regardless_of_server_default(lexical, tmp_path, monkeypatch):
    core, _, _ = lexical
    monkeypatch.setattr(core, "RETRIEVAL_MODE", "hybrid")
    calls = []
    search, prepare = core.search_papers_chroma, core.prepare_answer_payload
    monkeypatch.setattr(core, "search_papers_chroma",
                        lambda *a, **kw: calls.append(("search", kw["retrieval_mode"])) or search(*a, **kw))
    monkeypatch.setattr(core, "prepare_answer_payload",
                        lambda *a, **kw: calls.append(("packet", kw["retrieval_mode"])) or prepare(*a, **kw))
    suite = ev.load_suite(_write_suite(tmp_path / "s.jsonl", [
        {"query_id": "q1", "text": "240 mV overpotential",
         "evidence": [_span(core, "ATTB", 0, "240 mV at 10 mA cm-2", "e1")]}]))
    records = {name: ev.run_strategy(core, suite, name, ks=(1, 10), packet_budget=8000)
               for name in ("dense", "hybrid", "lexical")}
    assert {mode for _, mode in calls} == {"dense", "hybrid", "lexical"}
    assert ("search", "dense") in calls and ("packet", "dense") in calls
    assert records["hybrid"]["metrics"]["overall"]["span_coverage@1"] == 1.0
    assert records["lexical"]["metrics"]["overall"]["span_coverage@1"] == 1.0
    assert records["hybrid"]["strategy"]["params"] == {"retrieval_mode": "hybrid", "rerank": False}


def test_sidecar_build_rejects_chunks_whose_text_no_longer_matches(lexical):
    core, lexical_index, _ = lexical
    core.pdf_col.update(ids=["chunk-ATTB-0"], documents=["tampered text"], embeddings=[[0.0, 0.0, 0.0, 1.0]])
    with pytest.raises(lexical_index.LexicalIndexError, match="text hash mismatch"):
        lexical_index.build_sidecar(core.pdf_col, core.pdf_generation)


# -- precomputed query vectors, diagnostic depth, candidate pool ---------------------


def _suite(core, tmp_path):
    return ev.load_suite(_write_suite(tmp_path / "s.jsonl", [
        {"query_id": "q1", "text": "cobalt durability",
         "evidence": [_span(core, "ATTA", 1, "retains 95% activity", "e1")]},
        {"query_id": "q2", "text": "镍 过电位", "second_query": "nickel overpotential",
         "evidence": [_span(core, "ATTB", 0, "240 mV", "e2")]}]))


def test_precomputed_vectors_reproduce_live_scores_without_the_provider(core, tmp_path, monkeypatch):  # noqa: F811
    from test_retrieval_eval import _embed

    suite = _suite(core, tmp_path)
    live = ev.run_strategy(core, suite, "dense", ks=(1, 10), packet_budget=8000)
    contract = core.pdf_generation.manifest["contract"]["embedding"]
    data = ev.embed_queries(suite, _embed, lambda **kw: dict(contract))
    assert data["vectors"]["q2"]["text_sha256"] == ev.sha256_text("nickel overpotential")
    path = tmp_path / "vectors.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    vectors, digest = ev.load_query_vectors(path, suite, core.pdf_generation)

    def unavailable(_text):
        raise AssertionError("the embedding provider must not be called")

    monkeypatch.setattr(core, "embed_index_text", unavailable)
    monkeypatch.setattr(core, "embedding_contract", unavailable)
    offline = ev.run_strategy(core, suite, "dense", ks=(1, 10), packet_budget=8000,
                              query_vectors=vectors, query_vectors_sha256=digest)
    assert offline["metrics"]["overall"] == live["metrics"]["overall"]
    assert offline["query_vectors"] == {"precomputed": True, "sha256": digest}
    assert offline["latency_scope"] == "retrieval_only" and live["latency_scope"] == "end_to_end"


def test_query_vectors_are_bound_to_eval_set_and_embedding_contract(core, tmp_path):  # noqa: F811
    from test_retrieval_eval import _embed

    suite = _suite(core, tmp_path)
    contract = core.pdf_generation.manifest["contract"]["embedding"]
    path = tmp_path / "vectors.json"
    path.write_text(json.dumps(ev.embed_queries(suite, _embed, lambda **kw: {**contract, "revision": "r2"})))
    with pytest.raises(ev.SuiteError, match="embedding contract differs"):
        ev.load_query_vectors(path, suite, core.pdf_generation)
    path.write_text(json.dumps(ev.embed_queries(suite, _embed, lambda **kw: dict(contract))))
    edited = ev.load_suite(_write_suite(tmp_path / "edited.jsonl", [
        {**ev.dump_query(q), "text": q.text + "?"} if q.query_id == "q1" else ev.dump_query(q)
        for q in suite.queries]))
    with pytest.raises(ev.SuiteError, match="different version of the eval set"):
        ev.load_query_vectors(path, edited, core.pdf_generation)


def test_wrong_sized_query_vector_is_rejected(core):  # noqa: F811
    payload, status = core.search_papers_chroma("cobalt", query_vector=[1.0, 0.0])
    assert status == 409 and "dimensions" in payload["error"]


def test_diagnostic_depth_records_first_covered_rank_without_changing_scores(core, tmp_path):  # noqa: F811
    suite = _suite(core, tmp_path)
    plain = ev.run_strategy(core, suite, "dense", ks=(1,), packet_budget=None)
    deep = ev.run_strategy(core, suite, "dense", ks=(1,), packet_budget=None, diagnostic_depth=5)
    assert deep["metrics"]["overall"]["span_coverage@1"] == plain["metrics"]["overall"]["span_coverage@1"]
    assert deep["metrics"]["overall"]["span_coverage@5"] == 1.0
    ranks = [r for row in deep["per_query"].values() for r in row["span_first_covered_ranks"]]
    assert all(isinstance(r, int) and 1 <= r <= 5 for r in ranks)
    assert deep["settings"]["diagnostic_depth"] == 5 and plain["settings"]["diagnostic_depth"] is None


def test_hybrid_diagnostic_keeps_the_scored_candidate_pool(lexical, tmp_path, monkeypatch):
    core, _, _ = lexical
    monkeypatch.setattr(core, "HYBRID_CANDIDATES", 1)
    calls = []
    search = core.search_papers_chroma

    def recorded(*args, **kwargs):
        calls.append((kwargs.get("n"), kwargs.get("candidate_pool_depth")))
        return search(*args, **kwargs)

    monkeypatch.setattr(core, "search_papers_chroma", recorded)
    record = ev.run_strategy(core, _suite(core, tmp_path), "hybrid", ks=(1,),
                             packet_budget=None, diagnostic_depth=4)
    assert (1, None) in calls and (4, 1) in calls
    for row in record["per_query"].values():
        ranks = row["span_first_covered_ranks"]
        assert row["span_coverage@1"] == sum(rank is not None and rank <= 1 for rank in ranks) / len(ranks)


def test_diagnostic_rejects_a_changed_scored_prefix(lexical, tmp_path, monkeypatch):
    core, _, _ = lexical
    search = core.search_papers_chroma

    def changed(*args, **kwargs):
        payload, status = search(*args, **kwargs)
        if status == 200 and kwargs.get("n") == 4:
            payload["results"] = list(reversed(payload["results"]))
        return payload, status

    monkeypatch.setattr(core, "search_papers_chroma", changed)
    with pytest.raises(ev.SuiteError, match="diagnostic ranking diverges"):
        ev.run_strategy(core, _suite(core, tmp_path), "hybrid", ks=(1,),
                        packet_budget=None, diagnostic_depth=4)


def test_hybrid_candidate_pool_does_not_depend_on_requested_n(core, monkeypatch):  # noqa: F811
    monkeypatch.setattr(core, "HYBRID_CANDIDATES", 50)
    assert core._candidate_depth(3) == core._candidate_depth(10) == core._candidate_depth(20) == 50
    assert core._candidate_depth(80) == 80
