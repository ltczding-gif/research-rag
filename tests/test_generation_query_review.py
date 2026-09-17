from __future__ import annotations

import importlib
from pathlib import Path
import sys

import pytest


@pytest.fixture
def core(monkeypatch):
    pytest.importorskip("chromadb")
    names = (
        "config",
        "embedding_client",
        "index_generation",
        "generation_query",
        "query_server",
    )
    saved = {name: sys.modules.pop(name, None) for name in names}
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "service"))
    monkeypatch.setenv("LOCALRAG_SKIP_CHROMA_INIT", "1")
    try:
        yield importlib.import_module("query_server")
    finally:
        for name in names:
            sys.modules.pop(name, None)
            if saved[name] is not None:
                sys.modules[name] = saved[name]


class _Collection:
    def __init__(self, query_result=None, neighbor_result=None, count=1):
        self.query_result = query_result
        self.neighbor_result = neighbor_result
        self._count = count
        self.query_kwargs = None

    def count(self):
        return self._count

    def query(self, **kwargs):
        self.query_kwargs = kwargs
        return self.query_result

    def get(self, **_kwargs):
        return self.neighbor_result


def test_notes_only_health_is_ready(core, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "CHROMA_PATH", tmp_path)
    monkeypatch.setattr(core, "pdf_col", None)
    monkeypatch.setattr(core, "pdf_generation", None)
    monkeypatch.setattr(core, "chroma_ready", False)
    monkeypatch.setattr(core, "notes_col", _Collection(count=2))
    monkeypatch.setattr(core, "notes_generation", None)
    monkeypatch.setattr(core, "notes_ready", True)
    monkeypatch.setattr(core, "embedding_healthcheck", lambda: {"ok": True})

    response = core.app.test_client().get("/health")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "ok"
    assert payload["notes"]["ready"] is True
    assert payload["papers"]["ready"] is False


def test_canonical_context_does_not_depend_on_neighbor_chunk_copies(core, monkeypatch):
    metadata = {
        "generation_id": "g1",
        "file_id": "file-1",
        "next_chunk_id": "missing-neighbor",
        "previous_chunk_id": "",
    }
    collection = _Collection(
        query_result={
            "ids": [["hit-1"]],
            "documents": [["verified hit"]],
            "metadatas": [[metadata]],
            "distances": [[0.1]],
        },
        neighbor_result={"ids": [], "documents": [], "metadatas": []},
    )

    class Reader:
        manifest = {"contract": {"embedding": {"dimensions": 2}}}

        def check_embedding(self, _contract_fn):
            return None

        def evidence(self, _metadata, _content, chunk_id):
            return {"verified": True, "chunk_id": chunk_id}

        def context(self, _metadata, content, chunk_id):
            return {"context": f"[MATCH]{content}[/MATCH]",
                    "context_source": {"verified": True, "for_chunk_id": chunk_id}}

    monkeypatch.setattr(core, "pdf_col", collection)
    monkeypatch.setattr(core, "pdf_generation", Reader())
    monkeypatch.setattr(core, "chroma_ready", True)
    monkeypatch.setattr(core, "embed_index_text", lambda _text: [1.0, 0.0])
    monkeypatch.setattr(collection, "get", lambda **kwargs: pytest.fail("Canonical context must use page coordinates"))

    payload, status = core.search_papers_chroma(
        "query", include_context=True
    )

    assert status == 200
    assert payload["results"][0]["context_source"]["verified"] is True


def test_legacy_search_is_explicitly_unverified(core, monkeypatch):
    collection = _Collection(
        query_result={
            "ids": [["group_x_chunk_0"]],
            "documents": [["legacy text"]],
            "metadatas": [[{"chunk_index": 0}]],
            "distances": [[0.2]],
        }
    )
    monkeypatch.setattr(core, "pdf_col", collection)
    monkeypatch.setattr(core, "pdf_generation", None)
    monkeypatch.setattr(core, "chroma_ready", True)

    payload, status = core.search_papers_chroma("query")

    assert status == 200
    assert collection.query_kwargs["query_texts"] == ["query"]
    assert "query_embeddings" not in collection.query_kwargs
    assert payload["index_mode"] == "legacy_unverified"
    assert payload["results"][0]["evidence"]["verified"] is False
    assert "timing_note" in payload


@pytest.mark.parametrize("neighbor_meta", [
    {"pdf_path": "other.pdf", "zotero_parent_key": "OTHER"},
    {"pdf_path": "hit.pdf", "zotero_parent_key": "OTHER"},
    {"pdf_path": "hit.pdf", "source_sha256": "other-hash"},
    {"pdf_path": "hit.pdf", "file_hash": "other-hash"},
    {},
    None,
])
def test_legacy_context_omits_cross_source_or_unknown_neighbors(core, monkeypatch, neighbor_meta):
    metadata = {"pdf_path": "hit.pdf", "zotero_parent_key": "PARENT", "source_sha256": "hash", "file_hash": "hash"}
    collection = _Collection(
        query_result={
            "ids": [["group_443_file_0_chunk_20"]], "documents": [["hit"]],
            "metadatas": [[metadata]], "distances": [[0.2]],
        },
        neighbor_result={
            "ids": ["group_443_file_0_chunk_19", "group_443_file_0_chunk_21"],
            "documents": ["foreign text", "same-source next"],
            "metadatas": [neighbor_meta, metadata],
        },
    )
    monkeypatch.setattr(core, "pdf_col", collection)
    monkeypatch.setattr(core, "pdf_generation", None)
    monkeypatch.setattr(core, "chroma_ready", True)
    payload, status = core.search_papers_chroma("query", include_context=True)
    assert status == 200
    assert payload["results"][0]["context"] == "[MATCH]hit[/MATCH] same-source next"
    assert payload["results"][0]["evidence"]["verified"] is False


def test_mcp_forwards_canonical_attachment_filters(monkeypatch):
    pytest.importorskip("mcp")
    service = Path(__file__).resolve().parents[1] / "service"
    monkeypatch.syspath_prepend(str(service))
    monkeypatch.setenv("LOCALRAG_SKIP_CHROMA_INIT", "1")
    saved = {
        name: sys.modules.pop(name, None)
        for name in ("config", "query_server", "mcp_server")
    }
    try:
        module = importlib.import_module("mcp_server")
        received = []
        monkeypatch.setattr(
            module._core,
            "search_papers_chroma",
            lambda **kwargs: (received.append(kwargs) or {"results": []}, 200),
        )

        assert module.search_papers(
            "query",
            zotero_parent_key="PARENT",
            zotero_attachment_key="ATTACHMENT",
            source_role="si",
            pdf_filename="supplement.pdf",
        ) == {"results": []}
        assert received == [
            {
                "query": "query",
                "n": 3,
                "zotero_parent_key": "PARENT",
                "second_query": None,
                "include_context": True,
                "zotero_attachment_key": "ATTACHMENT",
                "source_role": "si",
                "pdf_filename": "supplement.pdf",
            }
        ]
    finally:
        for name, original in saved.items():
            sys.modules.pop(name, None)
            if original is not None:
                sys.modules[name] = original
