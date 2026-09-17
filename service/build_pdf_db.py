# ============================================================
# build_pdf_db.py — Batch PDF ingester (research papers / small PDFs)
#
# This script submits all chunks of a single PDF to ChromaDB in one
# col.add() call. Best for 20–80 page research papers.
#
# For large textbooks (200+ pages), use ingest_textbook.py instead;
# that script writes in batches of 50 chunks to avoid timeouts.
#
# All paths and tunables come from environment variables — see
# service/config.py and .env.example at the repo root.
# ============================================================
import argparse
import importlib.metadata
import json
import os
import re
import hashlib
import sqlite3 as zotero_sqlite
import glob
from pathlib import Path
import sys
import yaml

try:
    from .config import (
        NOTES_DIR,
        CHROMA_PATH,
        EMBED_PROVIDER,
        PAPERS_COLLECTION_NAME as COLLECTION_NAME,
        PDF_LEDGER as LEDGER_PATH,
        ZOTERO_DB_PATH as ZOTERO_DB,
        CHUNK_SIZE,
        CHUNK_STEP,
        MIN_CHUNK_LEN,
    )
    from .pdf_baseline import chunk_text, extract_text_pdfplumber
    from .pdf_sources import (
        canonical_chunk_start,
        PdfSourceError,
        PreparedPdfBuild,
        prepare_pdf_build,
        resolve_attachment_identity_exact,
        split_prepared_chunks_for_embedding,
    )
except ImportError:  # Support direct `python service/build_pdf_db.py` execution.
    from config import (
        NOTES_DIR,
        CHROMA_PATH,
        EMBED_PROVIDER,
        PAPERS_COLLECTION_NAME as COLLECTION_NAME,
        PDF_LEDGER as LEDGER_PATH,
        ZOTERO_DB_PATH as ZOTERO_DB,
        CHUNK_SIZE,
        CHUNK_STEP,
        MIN_CHUNK_LEN,
    )
    from pdf_baseline import chunk_text, extract_text_pdfplumber
    from pdf_sources import (
        canonical_chunk_start,
        PdfSourceError,
        PreparedPdfBuild,
        prepare_pdf_build,
        resolve_attachment_identity_exact,
        split_prepared_chunks_for_embedding,
    )


PAPERS_ID_SCHEMA = "content-hash-v1"
_file_hash_cache = {}


def get_file_hash(filepath, chunk_size=1024 * 1024):
    """Streamed SHA-256 of one PDF, memoized for this process."""
    abs_path = os.path.abspath(str(filepath))
    stat = os.stat(abs_path)
    cache_key = (abs_path, stat.st_size, stat.st_mtime_ns)
    if cache_key in _file_hash_cache:
        return _file_hash_cache[cache_key]

    h = hashlib.sha256()
    with open(abs_path, "rb") as f:
        for chunk in iter(lambda: f.read(chunk_size), b""):
            h.update(chunk)
    digest = h.hexdigest()
    _file_hash_cache[cache_key] = digest
    return digest


def build_chunk_ids(group_hash, file_hash, chunk_count):
    """Return IDs stable across note insertion and PDF-group reordering."""
    prefix = f"group_{group_hash}_file_{file_hash}_chunk_"
    return [f"{prefix}{index}" for index in range(chunk_count)]


def reset_papers_index(client, ledger_path, collection_name):
    """Reset only papers-index state after an explicit --rebuild request."""
    collection_names = {
        item if isinstance(item, str) else item.name
        for item in client.list_collections()
    }
    if collection_name in collection_names:
        client.delete_collection(collection_name)
    ledger_path.unlink(missing_ok=True)


def ensure_papers_id_schema(collection, expected_metadata):
    """Reject non-empty legacy collections; stamp empty collections safely."""
    collection_count = collection.count()
    collection_metadata = collection.metadata or {}
    existing_id_schema = collection_metadata.get("id_schema")
    if collection_count and existing_id_schema != PAPERS_ID_SCHEMA:
        raise SystemExit(
            "[FATAL] Existing papers collection uses the legacy positional "
            "chunk-ID schema. Re-run with --rebuild to migrate safely; this "
            "deletes only the papers collection and its ledger."
        )
    if not collection_count and existing_id_schema != PAPERS_ID_SCHEMA:
        collection.modify(metadata={**collection_metadata, **expected_metadata})


def get_parent_key_by_pdf_path(pdf_path, zotero_db=ZOTERO_DB):
    """Look up the Zotero parent item key for a PDF path.

    Tries the storage-key path first, then falls back to filename LIKE
    matching. KEEP IN SYNC with the canonical implementation at
    `scanner/zotero_client.py:get_parent_key`. Drift is caught by
    `tests/test_zotero_client_parity.py`.
    """
    try:
        conn = zotero_sqlite.connect(str(zotero_db))
        cursor = conn.cursor()

        # Strategy 1: exact attachment-key match from a storage path.
        m = re.search(
            r'[\\/]storage[\\/]([A-Za-z0-9]{8})[\\/]',
            os.path.abspath(pdf_path),
        )
        if m:
            attach_key = m.group(1)
            cursor.execute(
                """
                SELECT i_parent.key
                FROM itemAttachments ia
                JOIN items i_attach ON ia.itemID = i_attach.itemID
                JOIN items i_parent ON ia.parentItemID = i_parent.itemID
                WHERE i_attach.key = ?
                LIMIT 1
                """,
                (attach_key,),
            )
            row = cursor.fetchone()
            if row:
                conn.close()
                return row[0]

        # Strategy 2: filename LIKE fallback (approximate; for linked-file mode).
        filename = os.path.basename(pdf_path)
        cursor.execute(
            """
            SELECT i_parent.key
            FROM itemAttachments ia
            JOIN items i_attach ON ia.itemID = i_attach.itemID
            JOIN items i_parent ON ia.parentItemID = i_parent.itemID
            WHERE ia.path LIKE ?
            LIMIT 1
            """,
            (f"%{filename}%",),
        )
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else None
    except Exception as e:
        filename = os.path.basename(pdf_path)
        print(f"  [WARN] zotero_parent_key lookup failed for {filename}: {e}")
        return None

# --- Build PDF_GROUPS dynamically from note frontmatter ---

def extract_pdf_groups_from_notes(notes_dir):
    """Discover PDF groups by scanning note frontmatter for pdf_N_path keys."""
    pdf_groups = []
    for note_path in sorted(glob.glob(os.path.join(str(notes_dir), "*.md"))):
        with open(note_path, 'r', encoding='utf-8') as f:
            content = f.read()
        m = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
        if not m:
            print(f"  [SKIP] No frontmatter: {os.path.basename(note_path)}")
            continue
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except Exception as e:
            print(f"  [SKIP] YAML parse error in {os.path.basename(note_path)}: {e}")
            continue
        group = []
        i = 0
        while f'pdf_{i}_path' in fm:
            path = fm[f'pdf_{i}_path']
            if path and os.path.exists(path):
                group.append(path)
            elif path:
                print(f"  [WARN] PDF not found: {path}")
            i += 1
        if group:
            pdf_groups.append(group)
            print(f"  [OK] {os.path.basename(note_path)}: {len(group)} PDF(s)")
        else:
            print(f"  [SKIP] No valid PDF paths in: {os.path.basename(note_path)}")
    return pdf_groups

def get_combined_hash(file_paths):
    """Order-independent SHA-256 of one or more PDF files.

    KEEP IN SYNC with `scanner/_hashing.py:stable_combined_hash`. The
    `tests/test_hash_parity.py` smoke test imports both and asserts they
    produce identical output for the same input.

    Algorithm:
      1. Normalize input: dedup paths case-insensitively (Windows-friendly),
         sort lexicographically, drop nonexistent files.
      2. SHA-256 each remaining file.
      3. Sort the per-file hex digests lexicographically.
      4. Concatenate them as UTF-8 strings into a final SHA-256.

    Step 1 makes the result robust to duplicate or differently-cased
    paths. Step 3 makes the result independent of input order — same
    group of files produces the same hash regardless of main/SI order.

    Service-side scripts duplicate this logic instead of importing because
    `service/` and `scanner/` are deployable independently. If you change
    one, change both, and confirm `pytest tests/test_hash_parity.py` still
    passes.
    """
    # Step 1: normalize (matches scanner/_hashing.py:normalize_pdf_group_paths)
    seen = {}
    for path in file_paths:
        abs_path = os.path.abspath(str(path))
        seen.setdefault(abs_path.casefold(), abs_path)
    normalized = sorted(seen.values(), key=lambda v: v.casefold())

    # Step 2-3: per-file SHA-256, sorted
    file_hashes = []
    for path in normalized:
        if not os.path.exists(path):
            continue
        file_hashes.append(get_file_hash(path))
    file_hashes.sort()

    # Step 4: concatenate into final SHA-256
    combined = hashlib.sha256()
    for fh in file_hashes:
        combined.update(fh.encode("utf-8"))
    return combined.hexdigest()

def _parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Build the research-paper ChromaDB collection.")
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help=(
            "Build a fresh immutable candidate even when inputs match active. "
            "The active generation is never deleted first."
        ),
    )
    parser.add_argument(
        "--allow-removals",
        action="store_true",
        help="Allow a complete candidate to withdraw sources from the active generation.",
    )
    return parser.parse_args(argv)


def _canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _inventory_payload(plan):
    return [item.to_dict() for item in plan.inventory]


def _source_lookup(plan):
    return {
        item.file_id: item
        for item in plan.inventory
        if item.status == "success" and item.file_id
    }


def _validate_prepared_build(plan):
    if not plan.publishable:
        failures = [
            f"{item.note_path} pdf_{item.pdf_index}: {item.status}/{item.error_code}"
            for item in plan.inventory
            if item.status != "success"
        ]
        if not failures:
            failures.append("every note must declare a successful pdf_0 MAIN source")
        raise PdfSourceError("PDF source inventory is not publishable: " + "; ".join(failures))

    successful = _source_lookup(plan)
    documents = {document.file_id: document for document in plan.documents}
    if set(successful) != set(documents):
        raise PdfSourceError("successful inventory and canonical documents differ")
    if len({chunk.chunk_id for chunk in plan.chunks}) != len(plan.chunks):
        raise PdfSourceError("duplicate canonical chunk IDs")

    for file_id, source in successful.items():
        document = documents[file_id]
        if (
            document.paper_id != source.paper_id
            or document.file_hash != source.file_sha256
            or len(document.pages) != source.page_count
            or tuple(page.page_text_hash for page in document.pages)
            != source.page_text_hashes
        ):
            raise PdfSourceError(f"canonical document does not match inventory: {file_id}")

    chunk_ids = {chunk.chunk_id for chunk in plan.chunks}
    for chunk in plan.chunks:
        document = documents[chunk.file_id]
        canonical_chunk_start(document, chunk)
        for span in chunk.source_spans:
            page = document.pages[span.pdf_page_index]
            if page.page_text_hash != span.page_text_hash:
                raise PdfSourceError(f"chunk page hash mismatch: {chunk.chunk_id}")
        for neighbor in (chunk.previous_chunk_id, chunk.next_chunk_id):
            if neighbor and neighbor not in chunk_ids:
                raise PdfSourceError(f"chunk neighbor is not an actual chunk ID: {chunk.chunk_id}")


def _page_records(plan, generation_id):
    sources = _source_lookup(plan)
    for document in plan.documents:
        source = sources[document.file_id]
        for page in document.pages:
            yield {
                "generation_id": generation_id,
                "paper_id": document.paper_id,
                "file_id": document.file_id,
                "pdf_index": source.pdf_index,
                "source_role": source.source_role,
                "zotero_parent_key": source.zotero_parent_key,
                "zotero_attachment_key": source.zotero_attachment_key,
                "file_hash": document.file_hash,
                "extractor_fingerprint": document.extractor_fingerprint,
                "pdf_page_index": page.pdf_page_index,
                "printed_page_label": page.printed_page_label,
                "page_text_hash": page.page_text_hash,
                "normalized_text": page.normalized_text,
                "extraction_warnings": list(page.extraction_warnings),
            }


def _chunk_metadata(chunk, source, generation_id):
    return {
        "schema_version": chunk.schema_version,
        "generation_id": generation_id,
        "paper_id": chunk.paper_id,
        "file_id": chunk.file_id,
        "source_type": "pdf",
        "source_role": source.source_role,
        "pdf_index": source.pdf_index,
        "pdf_filename": source.pdf_filename or "",
        "pdf_path": source.declared_path or "",
        "is_main": chunk.is_main,
        "is_si": chunk.is_si,
        "file_hash": chunk.file_hash,
        "zotero_parent_key": source.zotero_parent_key or "",
        "zotero_attachment_key": source.zotero_attachment_key or "",
        "identity_source": source.identity_source,
        "start_page": chunk.start_page,
        "end_page": chunk.end_page,
        "text_hash": chunk.text_hash,
        "extractor_fingerprint": chunk.extractor_fingerprint,
        "chunker_fingerprint": chunk.chunker_fingerprint,
        "section_path_json": _canonical_json(list(chunk.section_path)),
        "source_spans_json": _canonical_json(
            [span.to_dict() for span in chunk.source_spans]
        ),
        "previous_chunk_id": chunk.previous_chunk_id or "",
        "next_chunk_id": chunk.next_chunk_id or "",
        "extraction_warnings_json": _canonical_json(
            list(chunk.extraction_warnings)
        ),
    }


def _write_candidate(
    *,
    client,
    store,
    generation,
    plan,
    embed_index_text,
    atomic_write_text,
):
    generation_id = generation["generation_id"]
    generation_path = store.generation_path(generation)
    pages_text = "".join(
        _canonical_json(record) + "\n"
        for record in _page_records(plan, generation_id)
    )
    atomic_write_text(generation_path / "pages.jsonl", pages_text)

    collection = client.create_collection(
        name=generation["collection_name"],
        metadata={
            "generation_id": generation_id,
            "id_schema": "canonical-pdf-v1",
            "hnsw:space": "cosine",
        },
    )
    sources = _source_lookup(plan)
    items = [
        (
            chunk.chunk_id,
            chunk.text,
            _chunk_metadata(chunk, sources[chunk.file_id], generation_id),
            embed_index_text(chunk.text),
        )
        for chunk in plan.chunks
    ]
    for offset in range(0, len(items), 100):
        batch = items[offset : offset + 100]
        collection.add(
            ids=[item[0] for item in batch],
            documents=[item[1] for item in batch],
            metadatas=[item[2] for item in batch],
            embeddings=[item[3] for item in batch],
        )
    expected_ids = {chunk.chunk_id for chunk in plan.chunks}
    stored = collection.get(ids=sorted(expected_ids), include=[])
    if collection.count() != len(plan.chunks) or set(stored["ids"]) != expected_ids:
        raise PdfSourceError("candidate collection does not contain every prepared chunk")


def _optional_env_path(name):
    value = os.environ.get(name, "").strip()
    return Path(os.path.expandvars(os.path.expanduser(value))) if value else None


def _build_contract(plan, embedding_contract, implementation_contract):
    inventory = _inventory_payload(plan)
    inventory_hash = hashlib.sha256(
        (_canonical_json(inventory) + "\n").encode("utf-8")
    ).hexdigest()
    implementation_files = [
        Path(__file__),
        Path(__file__).with_name("pdf_sources.py"),
        Path(__file__).with_name("pdf_ir.py"),
        Path(__file__).with_name("pdf_baseline.py"),
        Path(__file__).with_name("index_generation.py"),
    ]
    return {
        "embedding": embedding_contract(),
        "pipeline": implementation_contract(
            implementation_files,
            {
                "chunk_size": CHUNK_SIZE,
                "chunk_step": CHUNK_STEP,
                "min_chunk_len": MIN_CHUNK_LEN,
                "source_set_fingerprint": plan.source_set_fingerprint,
                "source_inventory_sha256": inventory_hash,
                "runtime_versions": {
                    "pdfplumber": importlib.metadata.version("pdfplumber"),
                },
                "extractor_fingerprints": sorted(
                    {document.extractor_fingerprint for document in plan.documents}
                ),
                "chunker_fingerprints": sorted(
                    {chunk.chunker_fingerprint for chunk in plan.chunks}
                ),
            },
        ),
    }


def _record_failed_attempt(store, generation, error, sources=None):
    if store is None:
        return "unknown"
    try:
        with store.writer_lock():
            attempt = generation or store.begin({}, sources or [])
            return store.fail(attempt, error)
    except Exception:
        # Preserve the original build error when status recording itself fails.
        return "unknown"


def _active_generation_usable(store, client, active):
    return bool(active and store.generation_usable(client, active))


def main(argv=None):
    generation = None
    store = None
    failure_sources = []
    try:
        args = _parse_args(argv)
        try:
            from .index_generation import (
                GenerationStore,
                atomic_write_text,
                implementation_contract,
            )
        except ImportError:
            from index_generation import (
                GenerationStore,
                atomic_write_text,
                implementation_contract,
            )

        store = GenerationStore(CHROMA_PATH, COLLECTION_NAME)
        plan = prepare_pdf_build(
            NOTES_DIR,
            zotero_db=ZOTERO_DB,
            zotero_data_dir=_optional_env_path("ZOTERO_DATA_DIR"),
            linked_attachment_base=_optional_env_path("ZOTERO_ATTACHMENT_BASE_DIR"),
            chunk_size=CHUNK_SIZE,
            chunk_step=CHUNK_STEP,
            min_chunk_len=MIN_CHUNK_LEN,
        )
        failure_sources = _inventory_payload(plan)
        if not plan.publishable:
            with store.writer_lock():
                generation = store.begin({}, failure_sources)
                _validate_prepared_build(plan)

        import chromadb
        try:
            from .embedding_client import (
                embedding_contract,
                embed_index_text,
                split_embedding_text,
            )
        except ImportError:
            from embedding_client import (
                embedding_contract,
                embed_index_text,
                split_embedding_text,
            )

        plan = split_prepared_chunks_for_embedding(plan, split_embedding_text)
        sources = _inventory_payload(plan)
        failure_sources = sources
        contract = _build_contract(plan, embedding_contract, implementation_contract)
        client = chromadb.PersistentClient(path=str(CHROMA_PATH))
        with store.writer_lock():
            active = store.load_active()
            if (
                plan.publishable
                and not args.rebuild
                and store.same_inputs(active, contract, sources)
                and _active_generation_usable(store, client, active)
            ):
                print(f"[SKIP] Active PDF generation already matches inputs: {active['generation_id']}")
                return 0
            generation = store.begin(contract, sources)
            _validate_prepared_build(plan)
            _write_candidate(
                client=client,
                store=store,
                generation=generation,
                plan=plan,
                embed_index_text=embed_index_text,
                atomic_write_text=atomic_write_text,
            )
            if active and not args.allow_removals:
                current_ids = {source["source_id"] for source in sources}
                removed = [source["source_id"] for source in active["sources"] if source["source_id"] not in current_ids]
                if removed:
                    raise PdfSourceError("Attachment withdrawals require --allow-removals: " + ", ".join(removed))
            store.publish(
                generation,
                item_count=len(plan.chunks),
                artifacts={"pages": "pages.jsonl"},
                allow_removals=args.allow_removals,
            )
            print(
                f"[OK] Published PDF generation {generation['generation_id']} "
                f"with {len(plan.chunks)} chunks"
            )
            return 0
    except KeyboardInterrupt as exc:
        outcome = _record_failed_attempt(store, generation, exc, failure_sources)
        if outcome == "committed":
            print("[COMMITTED WITH WARNING] PDF generation is active despite interruption; inspect status.", file=sys.stderr)
            return 3
        print("[INTERRUPTED] Inspect index status before retrying; publication outcome may be unknown.")
        return 130
    except Exception as exc:
        outcome = _record_failed_attempt(store, generation, exc, failure_sources)
        if outcome == "committed":
            print(f"[COMMITTED WITH WARNING] PDF generation is active: {exc}", file=sys.stderr)
            return 3
        print(f"[ERROR] PDF build stopped; inspect index status: {exc}")
        return 1

if __name__ == "__main__":
    sys.exit(main())
