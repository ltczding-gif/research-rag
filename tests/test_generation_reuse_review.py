from __future__ import annotations

from pathlib import Path
import sys


SERVICE_DIR = Path(__file__).resolve().parent.parent / "service"
sys.path.insert(0, str(SERVICE_DIR))
_module_names = (
    "config",
    "embedding_client",
    "index_generation",
    "build_notes_db",
    "build_pdf_db",
)
_saved_modules = {name: sys.modules.pop(name, None) for name in _module_names}
try:
    import build_notes_db as notes_builder  # noqa: E402
    import build_pdf_db as pdf_builder  # noqa: E402
    from index_generation import GenerationStore  # noqa: E402
finally:
    try:
        sys.path.remove(str(SERVICE_DIR))
    except ValueError:
        pass
    for name in _module_names:
        sys.modules.pop(name, None)
        if _saved_modules[name] is not None:
            sys.modules[name] = _saved_modules[name]


class FakeCollection:
    def __init__(self, metadata):
        self.metadata = metadata
        self.records = {}

    def upsert(self, *, ids, documents, embeddings, metadatas):
        for record_id, document, embedding, metadata in zip(
            ids, documents, embeddings, metadatas, strict=True
        ):
            self.records[record_id] = {
                "document": document,
                "embedding": embedding,
                "metadata": metadata,
            }

    def count(self):
        return len(self.records)


class FakeClient:
    def __init__(self):
        self.collections = {}

    def get_or_create_collection(self, *, name, metadata):
        return self.collections.setdefault(name, FakeCollection(metadata))

    def get_collection(self, name):
        return self.collections[name]


def _write_note(path: Path) -> None:
    path.write_text(
        "---\nzotero_parent_key: PARENT01\ntitle_en: Test\n---\n"
        "# Result\nStable content.\n",
        encoding="utf-8",
    )


def _split(text: str):
    return [(0, len(text), text)]


def _build_notes(notes_dir: Path, chroma_dir: Path, client: FakeClient):
    return notes_builder.build_notes_generation(
        notes_dir=notes_dir,
        chroma_path=chroma_dir,
        collection_name="notes-reuse-review",
        note_suffix="_review_note.md",
        client_factory=lambda **_ignored: client,
        embed_fn=lambda text: [float(len(text)), 1.0],
        split_fn=_split,
        embedding_contract_fn=lambda: {
            "provider": "fake",
            "model": "fixed",
            "revision": "test-v1",
            "dimensions": 2,
        },
    )


def test_notes_reuse_rebuilds_when_declared_note_artifact_is_missing(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    _write_note(notes_dir / "alpha_review_note.md")
    chroma_dir = tmp_path / "chroma"
    client = FakeClient()

    first, reused = _build_notes(notes_dir, chroma_dir, client)
    assert not reused
    store = GenerationStore(chroma_dir, "notes-reuse-review")
    relative = next(iter(first["artifacts"]["notes"].values()))
    (store.generation_path(first) / relative).unlink()

    repaired, reused = _build_notes(notes_dir, chroma_dir, client)

    assert not reused
    assert repaired["generation_id"] != first["generation_id"]
    assert store.validate_artifacts(repaired)


def test_pdf_reuse_rejects_matching_collection_when_pages_artifact_is_missing(
    tmp_path,
):
    store = GenerationStore(tmp_path / "chroma", "papers-reuse-review")
    sources = [
        {
            "source_id": "za-attach01",
            "source_hash": "a" * 64,
            "status": "success",
        }
    ]
    contract = {"embedding": {"provider": "fake", "model": "fixed"}}
    with store.writer_lock():
        candidate = store.begin(contract, sources)
        pages = store.generation_path(candidate) / "pages.jsonl"
        pages.write_text('{"page": 1}\n', encoding="utf-8")
        active = store.publish(
            candidate,
            item_count=1,
            artifacts={"pages": "pages.jsonl"},
        )

    client = FakeClient()
    collection = client.get_or_create_collection(
        name=active["collection_name"],
        metadata={"generation_id": active["generation_id"]},
    )
    collection.records["chunk-1"] = {}
    pages.unlink()

    assert not pdf_builder._active_generation_usable(store, client, active)
