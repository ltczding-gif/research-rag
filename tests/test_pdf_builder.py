from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import sys
import types

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO_ROOT / "service"
sys.path.insert(0, str(SERVICE_DIR))

_saved_config = sys.modules.pop("config", None)
try:
    import build_pdf_db  # noqa: E402
    from index_generation import GenerationStore  # noqa: E402
    from pdf_ir import (  # noqa: E402
        CanonicalDocument,
        DocumentPage,
        adapt_legacy_c0,
        hash_file,
    )
    from pdf_sources import (  # noqa: E402
        DeclaredPdfSource,
        PreparedPdfBuild,
        discover_declared_pdf_sources,
        prepare_pdf_build,
        resolve_attachment_identity_exact,
        split_prepared_chunks_for_embedding,
    )
finally:
    sys.modules.pop("config", None)
    if _saved_config is not None:
        sys.modules["config"] = _saved_config


def _write_note(notes: Path, name: str, metadata: dict[str, object]) -> Path:
    path = notes / name
    path.write_text(
        "---\n" + yaml.safe_dump(metadata, sort_keys=False) + "---\n",
        encoding="utf-8",
    )
    return path


def _fake_extractor(text_by_name: dict[str, tuple[str, ...]]):
    def extract(path, *, paper_id, file_id, expected_file_hash):
        pages = tuple(
            DocumentPage.create(
                paper_id=paper_id,
                file_id=file_id,
                pdf_page_index=index,
                text=text,
            )
            for index, text in enumerate(text_by_name[Path(path).name])
        )
        return CanonicalDocument(
            paper_id=paper_id,
            file_id=file_id,
            file_hash=expected_file_hash,
            extractor_fingerprint="e" * 64,
            pages=pages,
            extraction_warnings=("test-extractor",),
        )

    return extract


def _prepare(
    notes: Path,
    text_by_name: dict[str, tuple[str, ...]],
    *,
    chunk_size: int = 24,
    chunk_step: int = 20,
) -> PreparedPdfBuild:
    return prepare_pdf_build(
        notes,
        zotero_db=None,
        chunk_size=chunk_size,
        chunk_step=chunk_step,
        min_chunk_len=0,
        extractor=_fake_extractor(text_by_name),
    )


def _create_zotero_db(path: Path) -> Path:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE items (itemID INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE);
        CREATE TABLE itemAttachments (
            itemID INTEGER PRIMARY KEY,
            parentItemID INTEGER,
            path TEXT
        );
        """
    )
    rows = [
        (1, "PARENT01", 2, "ATTACH01", "attachments:first/paper.pdf"),
        (3, "PARENT02", 4, "ATTACH02", "attachments:second/paper.pdf"),
        (5, "PARENT03", 6, "STORE001", "storage:stored.pdf"),
    ]
    for parent_id, parent, attachment_id, attachment, zotero_path in rows:
        connection.execute("INSERT INTO items VALUES (?, ?)", (parent_id, parent))
        connection.execute("INSERT INTO items VALUES (?, ?)", (attachment_id, attachment))
        connection.execute(
            "INSERT INTO itemAttachments VALUES (?, ?, ?)",
            (attachment_id, parent_id, zotero_path),
        )
    connection.commit()
    connection.close()
    return path


def _declared(
    tmp_path: Path,
    *,
    original: Path,
    parent: str | None = None,
    attachment: str | None = None,
) -> DeclaredPdfSource:
    note = tmp_path / "note.md"
    note.write_text("note", encoding="utf-8")
    return DeclaredPdfSource(
        note_path=note,
        note_sha256=hash_file(note),
        pdf_index=0,
        source_role="main",
        declared_path=original,
        original_path=original,
        declared_parent_key=parent,
        declared_attachment_key=attachment,
    )


def test_sparse_declarations_preserve_indices_and_missing_main_blocks(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    si = tmp_path / "si.pdf"
    si.write_bytes(b"si")
    _write_note(
        notes,
        "paper.md",
        {
            "zotero_parent_key": "PARENT01",
            "pdf_2_path": str(si),
            "pdf_2_attachment_key": "ATTACH02",
        },
    )

    declarations = discover_declared_pdf_sources(notes)
    plan = _prepare(notes, {"si.pdf": ("supplement text",)})

    assert [(item.pdf_index, item.source_role) for item in declarations] == [(2, "si")]
    assert [(item.pdf_index, item.source_role) for item in plan.inventory] == [(2, "si")]
    assert not plan.publishable


def test_missing_si_retries_without_role_or_identity_drift(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    main = tmp_path / "main.pdf"
    si = tmp_path / "si.pdf"
    main.write_bytes(b"main")
    _write_note(
        notes,
        "paper.md",
        {
            "zotero_parent_key": "PARENT01",
            "pdf_0_path": str(main),
            "pdf_0_attachment_key": "ATTACH01",
            "pdf_1_path": str(si),
            "pdf_1_attachment_key": "ATTACH02",
        },
    )
    texts = {"main.pdf": ("main source text",), "si.pdf": ("si source text",)}

    missing = _prepare(notes, texts)
    si.write_bytes(b"si")
    recovered = _prepare(notes, texts)

    before = next(item for item in missing.inventory if item.pdf_index == 1)
    after = next(item for item in recovered.inventory if item.pdf_index == 1)
    assert before.status == "missing"
    assert after.status == "success"
    assert (before.paper_id, before.file_id, before.source_role) == (
        after.paper_id,
        after.file_id,
        after.source_role,
    )
    assert not missing.publishable
    assert recovered.publishable


def test_empty_and_extraction_error_sources_are_not_ingested(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    empty = tmp_path / "empty.pdf"
    broken = tmp_path / "broken.pdf"
    empty.write_bytes(b"empty")
    broken.write_bytes(b"broken")
    _write_note(
        notes,
        "empty.md",
        {
            "zotero_parent_key": "PARENT01",
            "pdf_0_path": str(empty),
            "pdf_0_attachment_key": "ATTACH01",
        },
    )
    _write_note(
        notes,
        "broken.md",
        {
            "zotero_parent_key": "PARENT02",
            "pdf_0_path": str(broken),
            "pdf_0_attachment_key": "ATTACH02",
        },
    )

    def extractor(path, **kwargs):
        if Path(path).name == "broken.pdf":
            raise ValueError("broken")
        return _fake_extractor({"empty.pdf": ("",)})(path, **kwargs)

    plan = prepare_pdf_build(
        notes,
        zotero_db=None,
        chunk_size=24,
        chunk_step=20,
        min_chunk_len=0,
        extractor=extractor,
    )

    assert {item.status for item in plan.inventory} == {"empty", "error"}
    assert plan.documents == ()
    assert plan.chunks == ()
    assert not plan.publishable


def test_declared_pair_is_exact_and_missing_db_is_not_created(tmp_path):
    db = _create_zotero_db(tmp_path / "zotero.sqlite")
    pdf = tmp_path / "paper.pdf"
    source = _declared(
        tmp_path,
        original=pdf,
        parent="PARENT01",
        attachment="ATTACH01",
    )

    matched = resolve_attachment_identity_exact(source, zotero_db=db)
    wrong = resolve_attachment_identity_exact(
        replace(source, declared_parent_key="PARENT02"), zotero_db=db
    )
    missing_db = tmp_path / "missing.sqlite"
    absent = resolve_attachment_identity_exact(source, zotero_db=missing_db)

    assert (matched.parent_key, matched.attachment_key) == ("PARENT01", "ATTACH01")
    assert wrong is None
    assert absent is None
    assert not missing_db.exists()


def test_exact_linked_and_storage_paths_never_use_basename_matching(tmp_path):
    db = _create_zotero_db(tmp_path / "zotero.sqlite")
    linked = tmp_path / "linked"
    first = linked / "first" / "paper.pdf"
    second = linked / "second" / "paper.pdf"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    storage = tmp_path / "storage" / "STORE001" / "stored.pdf"
    storage.parent.mkdir(parents=True)
    storage.write_bytes(b"stored")

    first_identity = resolve_attachment_identity_exact(
        _declared(tmp_path, original=first),
        zotero_db=db,
        linked_attachment_base=linked,
    )
    second_identity = resolve_attachment_identity_exact(
        _declared(tmp_path, original=second),
        zotero_db=db,
        linked_attachment_base=linked,
    )
    storage_identity = resolve_attachment_identity_exact(
        _declared(tmp_path, original=storage, parent="PARENT03"), zotero_db=db
    )
    wrong_storage_parent = resolve_attachment_identity_exact(
        _declared(tmp_path, original=storage, parent="PARENT01"), zotero_db=db
    )

    assert first_identity.attachment_key == "ATTACH01"
    assert second_identity.attachment_key == "ATTACH02"
    assert storage_identity.attachment_key == "STORE001"
    assert wrong_storage_parent is None


def test_copied_source_hash_mismatch_blocks_publication(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    copied = tmp_path / "copy.pdf"
    original = tmp_path / "original.pdf"
    copied.write_bytes(b"copy")
    original.write_bytes(b"different")
    _write_note(
        notes,
        "paper.md",
        {
            "zotero_parent_key": "PARENT01",
            "pdf_0_path": str(copied),
            "pdf_0_source_path": str(original),
            "pdf_0_attachment_key": "ATTACH01",
        },
    )

    plan = _prepare(notes, {"copy.pdf": ("unused",)})

    assert plan.inventory[0].status == "error"
    assert plan.inventory[0].error_code == "source_copy_hash_mismatch"
    assert plan.documents == ()
    assert not plan.publishable


def test_same_content_rename_preserves_semantic_fingerprint_and_chunk_ids(tmp_path):
    first_notes = tmp_path / "first-notes"
    second_notes = tmp_path / "second-notes"
    first_notes.mkdir()
    second_notes.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"same")
    metadata = {
        "zotero_parent_key": "PARENT01",
        "pdf_0_path": str(pdf),
        "pdf_0_attachment_key": "ATTACH01",
    }
    _write_note(first_notes, "old.md", metadata)
    _write_note(second_notes, "renamed.md", metadata)

    first = _prepare(first_notes, {"paper.pdf": ("stable canonical text",)})
    second = _prepare(second_notes, {"paper.pdf": ("stable canonical text",)})

    assert first.source_set_fingerprint == second.source_set_fingerprint
    assert [chunk.chunk_id for chunk in first.chunks] == [
        chunk.chunk_id for chunk in second.chunks
    ]
    assert first.inventory[0].note_path != second.inventory[0].note_path


def test_content_and_chunk_configuration_change_fingerprints(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"first")
    _write_note(
        notes,
        "paper.md",
        {
            "zotero_parent_key": "PARENT01",
            "pdf_0_path": str(pdf),
            "pdf_0_attachment_key": "ATTACH01",
        },
    )
    text = {"paper.pdf": ("0123456789abcdefghijklmnopqrstuvwxyz",)}
    initial = _prepare(notes, text, chunk_size=18, chunk_step=14)
    changed_config = _prepare(notes, text, chunk_size=16, chunk_step=12)
    pdf.write_bytes(b"second")
    changed_content = _prepare(notes, text, chunk_size=18, chunk_step=14)

    assert initial.source_set_fingerprint != changed_content.source_set_fingerprint
    assert {chunk.chunk_id for chunk in initial.chunks} != {
        chunk.chunk_id for chunk in changed_content.chunks
    }
    assert {chunk.chunk_id for chunk in initial.chunks} != {
        chunk.chunk_id for chunk in changed_config.chunks
    }


def test_embedding_window_split_preserves_tail_spans_and_actual_neighbors(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    _write_note(
        notes,
        "paper.md",
        {
            "zotero_parent_key": "PARENT01",
            "pdf_0_path": str(pdf),
            "pdf_0_attachment_key": "ATTACH01",
        },
    )
    base = _prepare(
        notes,
        {"paper.pdf": ("abcdefghij", "klmnopqrst")},
        chunk_size=100,
        chunk_step=100,
    )

    def split(text):
        return [
            (start, min(start + 7, len(text)), text[start : start + 7])
            for start in range(0, len(text), 7)
        ]

    adapted = split_prepared_chunks_for_embedding(base, split)

    assert "".join(chunk.text for chunk in adapted.chunks) == base.chunks[0].text
    assert len(adapted.chunks) == adapted.inventory[0].chunk_count == 3
    assert adapted.chunks[0].next_chunk_id == adapted.chunks[1].chunk_id
    assert adapted.chunks[1].previous_chunk_id == adapted.chunks[0].chunk_id
    assert adapted.chunks[1].next_chunk_id == adapted.chunks[2].chunk_id
    assert adapted.chunks[2].previous_chunk_id == adapted.chunks[1].chunk_id
    assert {span.pdf_page_index for span in adapted.chunks[1].source_spans} == {0, 1}
    build_pdf_db._validate_prepared_build(adapted)


def test_embedding_split_handles_leading_derived_page_newline(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    _write_note(
        notes,
        "paper.md",
        {
            "zotero_parent_key": "PARENT01",
            "pdf_0_path": str(pdf),
            "pdf_0_attachment_key": "ATTACH01",
        },
    )
    base = _prepare(
        notes,
        {"paper.pdf": ("abcdefg", "hijklmnopq")},
        chunk_size=100,
        chunk_step=100,
    )
    text = base.chunks[0].text
    adapted = split_prepared_chunks_for_embedding(
        base,
        lambda _: [(0, 7, text[:7]), (7, len(text), text[7:])],
    )

    assert adapted.chunks[1].text.startswith("\n")
    assert adapted.chunks[1].source_spans[0].pdf_page_index == 1
    build_pdf_db._validate_prepared_build(adapted)


def test_mixed_resolved_and_unresolved_inventory_remains_reportable(tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    good = tmp_path / "good.pdf"
    unresolved = tmp_path / "unresolved.pdf"
    good.write_bytes(b"good")
    unresolved.write_bytes(b"unresolved")
    _write_note(
        notes,
        "good.md",
        {
            "zotero_parent_key": "PARENT01",
            "pdf_0_path": str(good),
            "pdf_0_attachment_key": "ATTACH01",
        },
    )
    _write_note(notes, "unresolved.md", {"pdf_0_path": str(unresolved)})

    plan = _prepare(
        notes,
        {"good.pdf": ("good text",), "unresolved.pdf": ("not reached",)},
    )

    assert [item.status for item in plan.inventory] == ["error", "success"]
    assert len(plan.source_set_fingerprint) == 64
    assert not plan.publishable


def test_unavailable_or_empty_notes_root_fails_explicitly(tmp_path):
    with pytest.raises(ValueError, match="unavailable"):
        discover_declared_pdf_sources(tmp_path / "missing")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no declared PDF sources"):
        discover_declared_pdf_sources(empty)


def test_builder_writes_isolated_generation_pages_and_canonical_metadata(
    tmp_path, monkeypatch
):
    notes = tmp_path / "notes"
    notes.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    _write_note(
        notes,
        "paper.md",
        {
            "zotero_parent_key": "PARENT01",
            "pdf_0_path": str(pdf),
            "pdf_0_attachment_key": "ATTACH01",
        },
    )
    plan = _prepare(
        notes,
        {"paper.pdf": ("abcdefghij", "klmnopqrstuvwxyz0123456789")},
        chunk_size=18,
        chunk_step=14,
    )
    chroma_path = tmp_path / "chroma"
    fake_embedding = types.SimpleNamespace(
        embedding_contract=lambda: {"provider": "test", "model": "fixed-2d", "dimensions": 2},
        embed_index_text=lambda text: [float(len(text)), 1.0],
        split_embedding_text=lambda text: [(0, len(text), text)],
    )
    monkeypatch.setitem(sys.modules, "embedding_client", fake_embedding)
    monkeypatch.setattr(build_pdf_db, "prepare_pdf_build", lambda *args, **kwargs: plan)
    monkeypatch.setattr(build_pdf_db, "CHROMA_PATH", chroma_path)
    monkeypatch.setattr(build_pdf_db, "COLLECTION_NAME", "papers-test")
    monkeypatch.setattr(
        build_pdf_db,
        "get_parent_key_by_pdf_path",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy helper used")),
    )

    assert build_pdf_db.main([]) == 0

    store = GenerationStore(chroma_path, "papers-test")
    active = store.load_active()
    pages_path = store.generation_path(active) / "pages.jsonl"
    pages = [json.loads(line) for line in pages_path.read_text(encoding="utf-8").splitlines()]
    import chromadb

    client = chromadb.PersistentClient(path=str(chroma_path))
    collection = client.get_collection(active["collection_name"])
    stored = collection.get(include=["metadatas", "documents"])

    assert active["artifacts"] == {
        "pages": "pages.jsonl",
        "embedding_session": "embedding-session.json",
    }
    receipt = json.loads(
        (store.generation_path(active) / "embedding-session.json").read_text(encoding="utf-8")
    )
    assert receipt["request_count"] == len(plan.chunks)
    assert receipt["assurance"] == "observed-adapter-contract"
    assert receipt["bitwise_reproducibility_claim"] is False
    assert store.validate_artifacts(active)
    assert active["item_count"] == len(plan.chunks) == collection.count()
    assert collection.metadata["hnsw:space"] == "cosine"
    assert [page["pdf_page_index"] for page in pages] == [0, 1]
    assert {page["source_role"] for page in pages} == {"main"}
    assert set(stored["ids"]) == {chunk.chunk_id for chunk in plan.chunks}
    metadata = {item["file_id"]: item for item in stored["metadatas"]}
    assert set(metadata) == {"za-attach01"}
    for item in stored["metadatas"]:
        assert item["source_role"] == "main"
        assert item["zotero_parent_key"] == "PARENT01"
        assert json.loads(item["source_spans_json"])
        assert item["previous_chunk_id"] in {"", *stored["ids"]}
        assert item["next_chunk_id"] in {"", *stored["ids"]}

    first_generation = active["generation_id"]
    assert build_pdf_db.main([]) == 0
    assert store.load_active()["generation_id"] == first_generation
    assert len(client.list_collections()) == 1

    client.delete_collection(active["collection_name"])
    assert build_pdf_db.main([]) == 0
    repaired = store.load_active()
    assert repaired["generation_id"] != first_generation
    assert client.get_collection(repaired["collection_name"]).count() == len(plan.chunks)


def test_failed_complete_inventory_attempt_preserves_active_generation(
    tmp_path, monkeypatch
):
    notes = tmp_path / "notes"
    notes.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf")
    _write_note(
        notes,
        "paper.md",
        {
            "zotero_parent_key": "PARENT01",
            "pdf_0_path": str(pdf),
            "pdf_0_attachment_key": "ATTACH01",
        },
    )
    plan = _prepare(notes, {"paper.pdf": ("valid source content",)})
    failed = PreparedPdfBuild(
        inventory=(replace(plan.inventory[0], status="missing", error_code="source_missing"),),
        documents=(),
        chunks=(),
        source_set_fingerprint=plan.source_set_fingerprint,
    )
    plans = iter((plan, failed))
    chroma_path = tmp_path / "chroma"
    monkeypatch.setitem(
        sys.modules,
        "embedding_client",
        types.SimpleNamespace(
            embedding_contract=lambda: {"provider": "test", "model": "fixed-2d", "dimensions": 2},
            embed_index_text=lambda text: [float(len(text)), 1.0],
            split_embedding_text=lambda text: [(0, len(text), text)],
        ),
    )
    monkeypatch.setattr(build_pdf_db, "prepare_pdf_build", lambda *args, **kwargs: next(plans))
    monkeypatch.setattr(build_pdf_db, "CHROMA_PATH", chroma_path)
    monkeypatch.setattr(build_pdf_db, "COLLECTION_NAME", "papers-test")

    assert build_pdf_db.main([]) == 0
    store = GenerationStore(chroma_path, "papers-test")
    original = store.load_active()["generation_id"]
    assert build_pdf_db.main([]) == 1
    assert store.load_active()["generation_id"] == original
    attempt = store.latest_attempt()
    assert attempt["state"] == "failed"
    manifest = json.loads(
        (store.generation_path(attempt["generation_id"]) / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["sources"][0]["status"] == "missing"


def test_unavailable_root_records_failed_attempt_without_active(tmp_path, monkeypatch):
    chroma_path = tmp_path / "chroma"
    monkeypatch.setattr(build_pdf_db, "NOTES_DIR", tmp_path / "missing-notes")
    monkeypatch.setattr(build_pdf_db, "CHROMA_PATH", chroma_path)
    monkeypatch.setattr(build_pdf_db, "COLLECTION_NAME", "papers-test")

    assert build_pdf_db.main([]) == 1

    store = GenerationStore(chroma_path, "papers-test")
    assert store.load_active() is None
    assert store.latest_attempt()["state"] == "failed"


def test_builder_returns_130_on_interrupt(tmp_path, monkeypatch):
    chroma_path = tmp_path / "chroma"
    monkeypatch.setattr(build_pdf_db, "CHROMA_PATH", chroma_path)
    monkeypatch.setattr(build_pdf_db, "COLLECTION_NAME", "papers-test")
    monkeypatch.setattr(
        build_pdf_db,
        "prepare_pdf_build",
        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()),
    )
    assert build_pdf_db.main([]) == 130
    assert GenerationStore(chroma_path, "papers-test").latest_attempt()["state"] == "failed"
