from __future__ import annotations

from pathlib import Path
import sys

import pytest


SERVICE_DIR = Path(__file__).resolve().parent.parent / "service"
sys.path.insert(0, str(SERVICE_DIR))

_module_names = (
    "config",
    "embedding_client",
    "index_generation",
    "build_notes_db",
)
_saved_modules = {name: sys.modules.pop(name, None) for name in _module_names}
try:
    import build_notes_db as notes_builder  # noqa: E402
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
        for values in zip(ids, documents, embeddings, metadatas, strict=True):
            record_id, document, embedding, metadata = values
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
        if name not in self.collections:
            raise KeyError(name)
        return self.collections[name]


def _write_note(path: Path, parent_key: str, body: str) -> bytes:
    raw = (
        f"---\nzotero_parent_key: {parent_key}\ntitle_en: Test\n---\n"
        f"{body}"
    ).encode("utf-8")
    path.write_bytes(raw)
    return raw


def _split(text: str):
    return [
        (start, min(start + 11, len(text)), text[start : start + 11])
        for start in range(0, len(text), 11)
    ]


def _build(notes_dir: Path, chroma_dir: Path, client: FakeClient, **kwargs):
    contract_fn = kwargs.pop(
        "embedding_contract_fn",
        lambda: {
            "provider": "fake",
            "model": "fixed",
            "revision": "test-v1",
            "dimensions": 2,
        },
    )
    return notes_builder.build_notes_generation(
        notes_dir=notes_dir,
        chroma_path=chroma_dir,
        collection_name="notes-test",
        note_suffix="_review_note.md",
        client_factory=lambda **_ignored: client,
        embed_fn=kwargs.pop("embed_fn", lambda text: [float(len(text)), 1.0]),
        split_fn=kwargs.pop("split_fn", _split),
        embedding_contract_fn=contract_fn,
        **kwargs,
    )


def test_generation_preserves_full_note_and_section_offsets(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    source = notes_dir / "alpha_review_note.md"
    raw = _write_note(source, "PARENT01", "Intro.\n# First\nAlpha text.\n## Second\nBeta text.\n")
    client = FakeClient()

    manifest, reused = _build(notes_dir, tmp_path / "chroma", client)

    assert not reused
    assert manifest["state"] == "complete"
    note_id = notes_builder.note_document_id(source.name)
    relative = manifest["artifacts"]["notes"][note_id]
    artifact = GenerationStore(tmp_path / "chroma", "notes-test").generation_path(
        manifest
    ) / relative
    assert artifact.read_bytes() == raw

    collection = client.get_collection(manifest["collection_name"])
    assert collection.metadata == {
        "hnsw:space": "cosine",
        "embed_provider": "fake",
        "embed_model": "fixed",
        "generation_id": manifest["generation_id"],
    }
    ordered = sorted(
        collection.records.values(), key=lambda record: record["metadata"]["start"]
    )
    assert "".join(record["document"] for record in ordered) == raw.decode("utf-8")
    assert {record["metadata"]["section_title"] for record in ordered} == {
        "frontmatter",
        "First",
        "Second",
    }
    for record in ordered:
        metadata = record["metadata"]
        assert metadata["note_id"] == note_id
        assert metadata["source_file"] == source.name
        assert metadata["zotero_parent_key"] == "PARENT01"
        assert metadata["generation_id"] == manifest["generation_id"]
        assert metadata["embedding_truncated"] is False
        assert raw.decode("utf-8")[metadata["start"] : metadata["end"]] == record[
            "document"
        ]


def test_identical_snapshot_reuses_only_a_complete_matching_collection(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    _write_note(notes_dir / "alpha_review_note.md", "PARENT01", "# A\nText\n")
    client = FakeClient()
    calls = []

    first, reused = _build(
        notes_dir,
        tmp_path / "chroma",
        client,
        embed_fn=lambda text: calls.append(text) or [1.0, 2.0],
    )
    calls_after_first = len(calls)
    second, reused = _build(
        notes_dir,
        tmp_path / "chroma",
        client,
        embed_fn=lambda text: calls.append(text) or [1.0, 2.0],
    )

    assert reused
    assert second["generation_id"] == first["generation_id"]
    assert len(calls) == calls_after_first

    collection = client.get_collection(first["collection_name"])
    collection.records.pop(next(iter(collection.records)))
    repaired, reused = _build(notes_dir, tmp_path / "chroma", client)
    assert not reused
    assert repaired["generation_id"] != first["generation_id"]
    store = GenerationStore(tmp_path / "chroma", "notes-test")
    assert store.load_active()["generation_id"] == repaired["generation_id"]
    assert store.generation_usable(client, repaired)
    assert first["collection_name"] in client.collections


def test_embedding_contract_change_builds_a_new_generation(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    _write_note(notes_dir / "alpha_review_note.md", "PARENT01", "# A\nText\n")
    chroma_dir = tmp_path / "chroma"
    client = FakeClient()
    first, _ = _build(notes_dir, chroma_dir, client)

    second, reused = _build(
        notes_dir,
        chroma_dir,
        client,
        embedding_contract_fn=lambda: {
            "provider": "fake",
            "model": "fixed",
            "revision": "test-v2",
            "dimensions": 2,
        },
    )

    assert not reused
    assert second["generation_id"] != first["generation_id"]
    assert second["previous_generation"] == first["generation_id"]


def test_deletion_requires_opt_in_and_failed_attempt_keeps_active(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    first_path = notes_dir / "alpha_review_note.md"
    second_path = notes_dir / "beta_review_note.md"
    _write_note(first_path, "PARENT01", "# A\nAlpha\n")
    _write_note(second_path, "PARENT02", "# B\nBeta\n")
    chroma_dir = tmp_path / "chroma"
    client = FakeClient()
    first, _ = _build(notes_dir, chroma_dir, client)

    second_path.unlink()
    with pytest.raises(ValueError, match="--allow-removals"):
        _build(notes_dir, chroma_dir, client)

    store = GenerationStore(chroma_dir, "notes-test")
    assert store.load_active()["generation_id"] == first["generation_id"]
    assert store.latest_attempt()["state"] == "failed"

    with pytest.raises(notes_builder.NotesBuildError, match="declared dimensions"):
        _build(
            notes_dir,
            chroma_dir,
            client,
            embed_fn=lambda _text: [1.0],
        )
    assert store.load_active()["generation_id"] == first["generation_id"]

    replacement, reused = _build(
        notes_dir,
        chroma_dir,
        client,
        allow_removals=True,
    )
    assert not reused
    assert replacement["previous_generation"] == first["generation_id"]
    assert len(replacement["sources"]) == 1


def test_same_content_rename_publishes_a_clean_snapshot_without_removal_opt_in(
    tmp_path,
):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    original = notes_dir / "alpha_review_note.md"
    _write_note(original, "PARENT01", "# A\nAlpha\n")
    chroma_dir = tmp_path / "chroma"
    client = FakeClient()
    first, _ = _build(notes_dir, chroma_dir, client)

    renamed = original.with_name("renamed_review_note.md")
    original.rename(renamed)
    second, reused = _build(notes_dir, chroma_dir, client)

    assert not reused
    assert second["previous_generation"] == first["generation_id"]
    assert second["sources"][0]["source_id"] == notes_builder.note_document_id(
        renamed.name
    )
    collection = client.get_collection(second["collection_name"])
    assert {
        record["metadata"]["source_file"] for record in collection.records.values()
    } == {renamed.name}


def test_invalid_identity_or_embedding_failure_never_replaces_active(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    source = notes_dir / "alpha_review_note.md"
    _write_note(source, "PARENT01", "# A\nAlpha\n")
    chroma_dir = tmp_path / "chroma"
    client = FakeClient()
    first, _ = _build(notes_dir, chroma_dir, client)
    store = GenerationStore(chroma_dir, "notes-test")

    source.write_text("---\ntitle_en: Missing identity\n---\n# A\nChanged\n", encoding="utf-8")
    with pytest.raises(notes_builder.NotesBuildError, match="zotero_parent_key"):
        _build(notes_dir, chroma_dir, client)
    assert store.load_active()["generation_id"] == first["generation_id"]
    assert store.latest_attempt()["state"] == "failed"
    assert "zotero_parent_key" in store.latest_attempt()["error"]

    source.unlink()
    with pytest.raises(notes_builder.NotesBuildError, match="empty snapshot"):
        _build(notes_dir, chroma_dir, client)
    assert store.load_active()["generation_id"] == first["generation_id"]
    assert store.latest_attempt()["state"] == "failed"
    assert "empty snapshot" in store.latest_attempt()["error"]

    _write_note(source, "PARENT01", "# A\nChanged\n")
    with pytest.raises(RuntimeError, match="embedding failed"):
        _build(
            notes_dir,
            chroma_dir,
            client,
            embed_fn=lambda _text: (_ for _ in ()).throw(
                RuntimeError("embedding failed")
            ),
        )
    assert store.load_active()["generation_id"] == first["generation_id"]
    assert store.latest_attempt()["state"] == "failed"


def test_splitter_must_cover_each_section_exactly(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    _write_note(notes_dir / "alpha_review_note.md", "PARENT01", "# A\nAlpha\n")
    client = FakeClient()

    with pytest.raises(notes_builder.NotesBuildError, match="contiguous"):
        _build(
            notes_dir,
            tmp_path / "chroma",
            client,
            split_fn=lambda text: [(1, len(text), text[1:])],
        )


def test_interruption_marks_attempt_failed_and_keeps_active(tmp_path):
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    source = notes_dir / "alpha_review_note.md"
    _write_note(source, "PARENT01", "# A\nAlpha\n")
    chroma_dir = tmp_path / "chroma"
    client = FakeClient()
    first, _ = _build(notes_dir, chroma_dir, client)
    _write_note(source, "PARENT01", "# A\nChanged\n")

    with pytest.raises(KeyboardInterrupt):
        _build(
            notes_dir,
            chroma_dir,
            client,
            embed_fn=lambda _text: (_ for _ in ()).throw(KeyboardInterrupt()),
        )

    store = GenerationStore(chroma_dir, "notes-test")
    assert store.load_active()["generation_id"] == first["generation_id"]
    assert store.latest_attempt()["state"] == "failed"
    assert store.latest_attempt()["error"] == "interrupted"


def test_main_returns_failure_and_forwards_explicit_removal_flag(monkeypatch):
    def fail_build(**_kwargs):
        raise RuntimeError("candidate failed")

    monkeypatch.setattr(notes_builder, "build_notes_generation", fail_build)
    assert notes_builder.main([]) == 1

    received = []
    monkeypatch.setattr(
        notes_builder,
        "build_notes_generation",
        lambda **kwargs: (
            received.append(kwargs) or {
                "generation_id": "a" * 32,
                "item_count": 1,
            },
            False,
        ),
    )
    assert notes_builder.main(["--allow-removals"]) == 0
    assert received == [{"allow_removals": True}]
