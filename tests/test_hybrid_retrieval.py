"""Keyword sidecar, hybrid fusion and retrieval-mode contracts."""
from __future__ import annotations

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
    assert records["hybrid"]["strategy"]["params"] == {"retrieval_mode": "hybrid"}


def test_sidecar_build_rejects_chunks_whose_text_no_longer_matches(lexical):
    core, lexical_index, _ = lexical
    core.pdf_col.update(ids=["chunk-ATTB-0"], documents=["tampered text"], embeddings=[[0.0, 0.0, 0.0, 1.0]])
    with pytest.raises(lexical_index.LexicalIndexError, match="text hash mismatch"):
        lexical_index.build_sidecar(core.pdf_col, core.pdf_generation)
