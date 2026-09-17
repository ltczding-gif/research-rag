"""Serving contracts against isolated real Chroma collections and stored evidence."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import sys

import pytest


@pytest.fixture
def core(monkeypatch):
    pytest.importorskip("chromadb")
    names = ("config", "embedding_client", "index_generation", "generation_query", "query_server")
    saved = {name: sys.modules.pop(name, None) for name in names}
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "service"))
    monkeypatch.setenv("LOCALRAG_SKIP_CHROMA_INIT", "1")
    try:
        module = importlib.import_module("query_server")
        yield module
    finally:
        for name in names:
            sys.modules.pop(name, None)
            if saved[name] is not None:
                sys.modules[name] = saved[name]


def _hash(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture
def canonical(core, tmp_path, monkeypatch):
    import chromadb
    from generation_query import GenerationReader
    store = core.GenerationStore(tmp_path, "papers")
    embedding = {"model": "model-a", "revision": "r1", "dimensions": 2}
    identities = (("PARENT1", "MAIN1", "main"), ("PARENT1", "SI1", "si"), ("PARENT2", "SI2", "si"))
    sources = [{"source_id": f"file-{i}", "content_hash": str(i), "status": "success",
                "zotero_parent_key": parent, "zotero_attachment_key": attachment,
                "source_role": role, "pdf_filename": "same.pdf"}
               for i, (parent, attachment, role) in enumerate(identities)]
    manifest = store.begin({"embedding": embedding}, sources)
    client = chromadb.PersistentClient(path=str(tmp_path))
    collection = client.create_collection(manifest["collection_name"],
                                         metadata={"hnsw:space": "cosine", "generation_id": manifest["generation_id"]})
    records, metas = [], []
    for i, (parent, attachment, role) in enumerate(identities):
        common = {"generation_id": manifest["generation_id"], "paper_id": parent,
                  "file_id": f"file-{i}", "file_hash": str(i), "extractor_fingerprint": "extractor-1",
                  "zotero_parent_key": parent, "zotero_attachment_key": attachment,
                  "source_role": role}
        text = f"Evidence from {attachment}"
        records.append({**common, "pdf_page_index": 0, "normalized_text": text,
                        "page_text_hash": _hash(text)})
        span = {"file_id": f"file-{i}", "pdf_page_index": 0,
                "char_start_in_normalized_page": 0, "char_end_in_normalized_page": len(text),
                "page_text_hash": _hash(text)}
        meta = {**common, "text_hash": _hash(text), "source_spans_json": json.dumps([span]),
                "source_type": "pdf", "pdf_filename": "same.pdf", "previous_chunk_id": "", "next_chunk_id": ""}
        metas.append(meta)
        collection.add(ids=[f"opaque-{i}"], documents=[text], embeddings=[[1.0, 0.1 * i]], metadatas=[meta])
    core.atomic_write_text(store.generation_path(manifest) / "pages.jsonl",
                           "\n".join(json.dumps(row) for row in records))
    store.publish(manifest, item_count=3, artifacts={"pages": "pages.jsonl"})
    reader = GenerationReader(store, manifest)
    monkeypatch.setattr(core, "pdf_col", collection)
    monkeypatch.setattr(core, "pdf_generation", reader)
    monkeypatch.setattr(core, "chroma_ready", True)
    monkeypatch.setattr(core, "CHROMA_PATH", tmp_path)
    monkeypatch.setattr(core, "embedding_contract", lambda **kw: dict(embedding))
    monkeypatch.setattr(core, "embed_index_text", lambda text: [1.0, 0.0])
    return core, store, reader, collection, records, metas, embedding


def test_parent_attachment_role_are_combined_in_real_chroma(canonical):
    core, _, _, _, _, _, _ = canonical
    payload, status = core.search_papers_chroma("evidence", zotero_parent_key="PARENT1",
                                               zotero_attachment_key="SI1", source_role="si",
                                               pdf_filename="same.pdf", include_context=True)
    assert status == 200
    assert [row["id"] for row in payload["results"]] == ["opaque-1"]
    evidence = payload["results"][0]["evidence"]
    assert evidence["verified"] and evidence["segments"][0]["page_number"] == 1
    assert evidence["segments"][0]["quote"] == "Evidence from SI1"
    assert set(payload["timings_seconds"]) == {"query_embedding", "retrieval", "rerank", "serialization", "total"}
    conflict, status = core.search_papers_chroma("evidence", zotero_parent_key="PARENT1", zotero_attachment_key="SI2")
    assert status == 400 and "conflicts" in conflict["error"]
    empty, status = core.search_papers_chroma("evidence", zotero_parent_key="PARENT1", zotero_attachment_key="UNKNOWN")
    assert status == 200 and empty["results"] == []


def test_answer_workflow_routes_use_real_canonical_sources(canonical):
    core = canonical[0]
    http = core.app.test_client()
    response = http.post('/prepare_answer', json={'query': 'evidence', 'n': 1,
                         'zotero_attachment_key': 'MAIN1', 'budget_codepoints': 8000})
    assert response.status_code == 200
    packet = response.get_json()
    assert packet['used_codepoints'] == len('Evidence from MAIN1')
    assert packet['evidence'][0]['source']['zotero_attachment_key'] == 'MAIN1'
    answer = {'claims': [{'text': 'Evidence from MAIN1', 'citations': [{'evidence_id': 'E1'}]}],
              'missing_information': []}
    checked = http.post('/check_answer', json={'packet': packet, 'answer': answer})
    assert checked.status_code == 200
    assert checked.get_json()['citation_checks_passed']
    assert checked.get_json()['claim_bindings'][0]['quote'] == 'Evidence from MAIN1'
    assert checked.get_json()['semantic_support'] == 'requires_review'
    answer['claims'][0]['citations'][0]['evidence_id'] = 'E99'
    assert not http.post('/check_answer', json={'packet': packet, 'answer': answer}).get_json()['citation_checks_passed']
    assert http.post('/check_answer', json={}).status_code == 400
    assert http.post('/prepare_answer', json={'query': 'evidence', 'budget_codepoints': 8001}).status_code == 400
    assert http.post('/prepare_answer', json={'query': 123}).status_code == 400
    assert http.post('/prepare_answer', json=['bad']).status_code == 400


def test_answer_workflow_refuses_legacy_but_handles_empty_canonical_results(canonical, monkeypatch):
    core = canonical[0]
    packet, status = core.prepare_answer_payload('evidence', n=1, zotero_parent_key='MISSING')
    assert status == 200 and packet['status'] == 'insufficient_evidence'
    monkeypatch.setattr(core, 'pdf_generation', None)
    assert core.prepare_answer_payload('evidence')[1] == 409


@pytest.mark.parametrize("filters", [{"source_role": "main"}, {"pdf_filename": "other.pdf"}])
def test_known_attachment_rejects_conflicting_source_filters(canonical, filters):
    core, _, _, _, _, _, _ = canonical
    result, status = core.search_papers_chroma("evidence", zotero_attachment_key="SI1", **filters)
    assert status == 400 and "conflicts" in result["error"]


def test_same_dimension_model_swap_blocks_query_and_readiness(canonical, monkeypatch):
    core, _, _, _, _, _, embedding = canonical
    monkeypatch.setattr(core, "embedding_contract", lambda **kw: {**embedding, "model": "different"})
    payload, status = core.search_papers_chroma("evidence")
    assert status == 409 and "contract changed" in payload["error"]
    assert core.index_state()["papers"]["ready"] is False


def test_changed_page_cannot_be_returned_as_verified(canonical):
    core, store, reader, _, records, _, _ = canonical
    records[0]["normalized_text"] = "tampered page"
    core.atomic_write_text(store.generation_path(reader.manifest) / "pages.jsonl", "\n".join(json.dumps(row) for row in records))
    payload, status = core.search_papers_chroma("evidence", zotero_attachment_key="MAIN1")
    assert status == 409 and "page text hash mismatch" in payload["error"]


def test_span_cannot_point_at_an_unrelated_valid_quote(canonical):
    core, _, _, collection, _, metas, _ = canonical
    metadata = dict(metas[0])
    span = json.loads(metadata["source_spans_json"])[0]
    span["char_end_in_normalized_page"] = 3
    metadata["source_spans_json"] = json.dumps([span])
    collection.update(ids=["opaque-0"], metadatas=[metadata])
    payload, status = core.search_papers_chroma("evidence", zotero_attachment_key="MAIN1")
    assert status == 409 and "reconstruct" in payload["error"]


def test_failed_attempt_leaves_active_ready_and_exposes_failure(canonical):
    core, store, reader, _, _, _, _ = canonical
    failed = store.begin({}, [])
    store.fail(failed, "missing SI")
    state = core.index_state()["papers"]
    assert state["ready"] and state["generation_id"] == reader.manifest["generation_id"]
    assert state["latest_attempt"]["state"] == "failed"
    assert state["latest_attempt"]["error"] == "missing SI"


def test_invalid_role_and_legacy_ordinal_are_explicit(canonical):
    core = canonical[0]
    assert core.search_papers_chroma("evidence", source_role="supplement")[1] == 400
    assert core.search_papers_chroma("evidence", paper_group=1)[1] == 400


def test_cross_page_context_deduplicates_overlapping_neighbor_text(canonical):
    core, store, reader, collection, records, metas, _ = canonical
    first = records[0]
    second = {**first, "pdf_page_index": 1, "normalized_text": "continues here", "page_text_hash": _hash("continues here")}
    records.append(second)
    content = first["normalized_text"] + "\n" + second["normalized_text"]
    spans = [{"file_id": "file-0", "pdf_page_index": page["pdf_page_index"],
              "char_start_in_normalized_page": 0, "char_end_in_normalized_page": len(page["normalized_text"]),
              "page_text_hash": page["page_text_hash"]} for page in (first, second)]
    meta = {**metas[0], "text_hash": _hash(content), "source_spans_json": json.dumps(spans),
            "next_chunk_id": "opaque-neighbor"}
    neighbor = {**metas[0], "text_hash": second["page_text_hash"], "source_spans_json": json.dumps([spans[1]])}
    collection.update(ids=["opaque-0"], documents=[content], embeddings=[[1., 0.]], metadatas=[meta])
    collection.add(ids=["opaque-neighbor"], documents=[second["normalized_text"]], embeddings=[[0., 1.]], metadatas=[neighbor])
    core.atomic_write_text(store.generation_path(reader.manifest) / "pages.jsonl", "\n".join(json.dumps(row) for row in records))
    result, status = core.search_papers_chroma("evidence", n=1, zotero_attachment_key="MAIN1", include_context=True)
    assert status == 200
    hit = result["results"][0]
    assert [part["page_number"] for part in hit["evidence"]["segments"]] == [1, 2]
    assert hit["context"] == f"[MATCH]{content}[/MATCH]"
    assert [part["page_number"] for part in hit["context_source"]["segments"]] == [1, 2]
    assert hit["context_source"]["for_chunk_id"] == "opaque-0"


def test_full_note_reads_original_bytes_and_combines_filters(core, tmp_path, monkeypatch):
    import chromadb
    from generation_query import GenerationReader
    text = "---\r\nzotero_parent_key: P1\r\n---\r\n# First\r\nbody\r\n# Tail\r\n末尾条件不可省略。"
    store = core.GenerationStore(tmp_path, "notes")
    manifest = store.begin({"embedding": {"dimensions": 2}}, [{"source_id": "note1", "content_hash": _hash(text), "status": "success"}])
    path = store.generation_path(manifest) / "note1.md"
    path.write_bytes(text.encode())
    client = chromadb.PersistentClient(path=str(tmp_path))
    collection = client.create_collection(manifest["collection_name"])
    for index, (start, end) in enumerate(((0, 35), (35, len(text)))):
        collection.add(ids=[f"section-{index}"], documents=[text[start:end]], embeddings=[[1., 0.]],
                       metadatas=[{"generation_id": manifest["generation_id"], "note_id": "note1", "source_file": "test.md",
                                   "zotero_parent_key": "P1", "start": start, "end": end}])
    store.publish(manifest, item_count=2, artifacts={"notes": {"note1": "note1.md"}})
    monkeypatch.setattr(core, "notes_col", collection)
    monkeypatch.setattr(core, "notes_generation", GenerationReader(store, manifest))
    monkeypatch.setattr(core, "notes_ready", True)
    payload, status = core.get_note_payload(source="test.md", zotero_parent_key="P1")
    assert status == 200 and len(payload["notes"]) == 1
    assert payload["notes"][0]["content"] == text
    assert core.get_note_payload(source="different.md", zotero_parent_key="P1")[1] == 404
    path.write_bytes(b"changed")
    assert core.get_note_payload(source="test.md")[1] == 500
