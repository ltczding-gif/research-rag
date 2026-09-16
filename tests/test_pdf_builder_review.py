"""Independent regressions for PDF generation provenance contracts."""

from __future__ import annotations

import json
from pathlib import Path
import sys
import types

import pytest
import yaml


REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO_ROOT / "service"
_MODULE_NAMES = (
    "config",
    "pdf_baseline",
    "pdf_ir",
    "pdf_sources",
    "index_generation",
    "build_pdf_db",
    "generation_query",
)
_SAVED_MODULES = {name: sys.modules.pop(name, None) for name in _MODULE_NAMES}
sys.path.insert(0, str(SERVICE_DIR))
try:
    import build_pdf_db  # noqa: E402
    from generation_query import GenerationReader  # noqa: E402
    from index_generation import GenerationStore, implementation_contract  # noqa: E402
    from pdf_ir import CanonicalDocument, DocumentPage, hash_file  # noqa: E402
    from pdf_sources import (  # noqa: E402
        prepare_pdf_build,
        split_prepared_chunks_for_embedding,
    )
finally:
    try:
        sys.path.remove(str(SERVICE_DIR))
    except ValueError:
        pass
    for name in _MODULE_NAMES:
        sys.modules.pop(name, None)
        if _SAVED_MODULES[name] is not None:
            sys.modules[name] = _SAVED_MODULES[name]


def _prepared(
    tmp_path: Path,
    page_texts: tuple[str, ...],
    *,
    attachment_key: str = "ATTACH01",
):
    tmp_path.mkdir(parents=True, exist_ok=True)
    notes = tmp_path / "notes"
    notes.mkdir()
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"pdf-review-fixture")
    (notes / "paper.md").write_text(
        "---\n"
        + yaml.safe_dump(
            {
                "zotero_parent_key": "PARENT01",
                "pdf_0_path": str(pdf),
                "pdf_0_attachment_key": attachment_key,
            },
            sort_keys=False,
        )
        + "---\n",
        encoding="utf-8",
    )

    def extractor(path, *, paper_id, file_id, expected_file_hash):
        assert hash_file(path) == expected_file_hash
        return CanonicalDocument(
            paper_id=paper_id,
            file_id=file_id,
            file_hash=expected_file_hash,
            extractor_fingerprint="e" * 64,
            pages=tuple(
                DocumentPage.create(
                    paper_id=paper_id,
                    file_id=file_id,
                    pdf_page_index=index,
                    text=text,
                )
                for index, text in enumerate(page_texts)
            ),
        )

    return prepare_pdf_build(
        notes,
        zotero_db=None,
        chunk_size=100,
        chunk_step=100,
        min_chunk_len=0,
        extractor=extractor,
    )


class _ReaderStore:
    def __init__(self, directory: Path):
        self.directory = directory

    def generation_path(self, _manifest):
        return self.directory


def _assert_reader_accepts_every_chunk(tmp_path: Path, plan) -> None:
    generation_id = "review-generation"
    pages_path = tmp_path / "pages.jsonl"
    pages_path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            for record in build_pdf_db._page_records(plan, generation_id)
        ),
        encoding="utf-8",
    )
    reader = GenerationReader(
        _ReaderStore(tmp_path),
        {
            "generation_id": generation_id,
            "artifacts": {"pages": "pages.jsonl"},
        },
    )
    source = next(item for item in plan.inventory if item.status == "success")
    for chunk in plan.chunks:
        metadata = build_pdf_db._chunk_metadata(chunk, source, generation_id)
        assert reader.evidence(metadata, chunk.text, chunk.chunk_id)["verified"]


def _assert_every_real_page_character_is_covered(plan) -> None:
    documents = {document.file_id: document for document in plan.documents}
    for file_id, document in documents.items():
        covered = {
            page.pdf_page_index: set()
            for page in document.pages
        }
        for chunk in plan.chunks:
            if chunk.file_id != file_id:
                continue
            for span in chunk.source_spans:
                covered[span.pdf_page_index].update(
                    range(
                        span.char_start_in_normalized_page,
                        span.char_end_in_normalized_page,
                    )
                )
        for page in document.pages:
            assert covered[page.pdf_page_index] == set(
                range(len(page.normalized_text))
            )


@pytest.mark.parametrize(
    ("page_texts", "bounds"),
    (
        (("abc", "def"), (0, 3, 7)),
        (("abc", "def"), (0, 4, 7)),
        (("abc", "", "def"), (0, 3, 5, 8)),
    ),
    ids=("leading-separator", "trailing-separator", "empty-page-separators"),
)
def test_embedding_split_chunks_round_trip_through_generation_reader(
    tmp_path, page_texts, bounds
):
    base = _prepared(tmp_path, page_texts)
    assert len(base.chunks) == 1
    original = base.chunks[0].text

    def split(_text):
        return [
            (start, end, original[start:end])
            for start, end in zip(bounds, bounds[1:])
        ]

    adapted = split_prepared_chunks_for_embedding(base, split)

    _assert_every_real_page_character_is_covered(adapted)
    _assert_reader_accepts_every_chunk(tmp_path, adapted)


def test_pdf_pipeline_contract_binds_reference_truncation_implementation(tmp_path):
    plan = _prepared(tmp_path, ("body without a reference heading",))

    contract = build_pdf_db._build_contract(
        plan,
        lambda: {"provider": "test", "model": "fixed"},
        implementation_contract,
    )

    implementations = contract["pipeline"]["implementation"]
    assert implementations["pdf_baseline.py"] == hash_file(
        SERVICE_DIR / "pdf_baseline.py"
    )


def test_same_bytes_new_attachment_identity_requires_explicit_withdrawal(
    tmp_path, monkeypatch
):
    monkeypatch.syspath_prepend(str(SERVICE_DIR))
    first = _prepared(
        tmp_path / "first",
        ("same canonical text",),
        attachment_key="ATTACH01",
    )
    replacement = _prepared(
        tmp_path / "replacement",
        ("same canonical text",),
        attachment_key="ATTACH02",
    )
    plans = iter((first, replacement, replacement))
    chroma_path = tmp_path / "chroma"
    monkeypatch.setitem(
        sys.modules,
        "embedding_client",
        types.SimpleNamespace(
            embedding_contract=lambda: {
                "provider": "test",
                "model": "fixed-2d",
                "dimensions": 2,
            },
            embed_index_text=lambda text: [float(len(text)), 1.0],
            split_embedding_text=lambda text: [(0, len(text), text)],
        ),
    )
    monkeypatch.setattr(
        build_pdf_db,
        "prepare_pdf_build",
        lambda *args, **kwargs: next(plans),
    )
    monkeypatch.setattr(build_pdf_db, "CHROMA_PATH", chroma_path)
    monkeypatch.setattr(build_pdf_db, "COLLECTION_NAME", "papers-review")

    assert build_pdf_db.main([]) == 0
    store = GenerationStore(chroma_path, "papers-review")
    original = store.load_active()

    assert build_pdf_db.main([]) == 1
    assert store.load_active()["generation_id"] == original["generation_id"]
    assert "Attachment withdrawals require --allow-removals" in (
        store.latest_attempt()["error"]
    )

    assert build_pdf_db.main(["--allow-removals"]) == 0
    assert store.load_active()["sources"][0]["source_id"] == "za-attach02"
