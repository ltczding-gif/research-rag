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
import os
import re
import hashlib
import sqlite3 as zotero_sqlite
import glob
import yaml

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
            "Delete and rebuild only the papers collection and its ledger. "
            "Required once when migrating from the legacy positional chunk-ID schema."
        ),
    )
    return parser.parse_args(argv)


def main(argv=None):
    import chromadb

    from embedding_client import active_model_id, get_chromadb_embedding_function

    args = _parse_args(argv)

    print("[INIT] Extracting PDF groups from notes...")
    pdf_groups = extract_pdf_groups_from_notes(NOTES_DIR)
    print(f"[INIT] Total groups: {len(pdf_groups)}")

    # Init ChromaDB before loading the ledger so an explicit rebuild can reset
    # both pieces of papers-index state together. The notes collection is not
    # touched.
    ef = get_chromadb_embedding_function()
    CHROMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))

    if args.rebuild:
        reset_papers_index(client, LEDGER_PATH, COLLECTION_NAME)
        print(f"[REBUILD] Reset papers collection '{COLLECTION_NAME}' and ledger")

    # Load ledger (group-level resume support)
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LEDGER_PATH.exists():
        with open(LEDGER_PATH, 'r', encoding='utf-8') as f:
            processed = set(line.strip() for line in f)
    else:
        processed = set()

    expected_metadata = {
        "embed_provider": EMBED_PROVIDER,
        "embed_model": active_model_id(),
        "id_schema": PAPERS_ID_SCHEMA,
    }
    col = client.get_or_create_collection(
        name=COLLECTION_NAME,
        embedding_function=ef,
        # Stamped at first creation only — records what the collection was
        # actually built with, so provider/model switches are diagnosable.
        metadata=expected_metadata,
    )
    ensure_papers_id_schema(col, expected_metadata)

    total_groups = len(pdf_groups)
    print(f"Total groups: {total_groups}")
    print(f"Already processed: {len(processed)}")
    print(f"Remaining: {total_groups - len(processed)}")

    for group_idx, pdf_group in enumerate(pdf_groups, 1):
        # 计算本组组合hash
        group_hash = get_combined_hash(pdf_group)
        
        if group_hash in processed:
            print(f"\n[Group {group_idx}/{total_groups}] SKIP (already processed)")
            continue
        
        print(f"\n[Group {group_idx}/{total_groups}] Processing {len(pdf_group)} PDF(s)...")
        
        # 查询 Zotero parent_key（用主文PDF查）
        parent_key = get_parent_key_by_pdf_path(pdf_group[0]) if pdf_group else None
        if parent_key:
            print(f"  [Zotero] parent_key: {parent_key}")
        
        group_chunks_count = 0
        group_had_errors = False

        for file_idx, pdf_path in enumerate(pdf_group):
            if not os.path.exists(pdf_path):
                print(f"  [SKIP] File not found: {os.path.basename(pdf_path)}")
                continue
            
            filename = os.path.basename(pdf_path)
            is_main = (file_idx == 0)
            doc_type = "MAIN" if is_main else "SI"
            print(f"  - [{doc_type}] {filename}")
            
            try:
                # 提取文本（自动截断参考文献）
                full_text = extract_text_pdfplumber(pdf_path)
                
                if not full_text.strip():
                    print(f"    [WARNING] No text extracted")
                    continue
                
                # 分块
                chunks = chunk_text(
                    full_text,
                    chunk_size=CHUNK_SIZE,
                    chunk_step=CHUNK_STEP,
                    min_chunk_len=MIN_CHUNK_LEN,
                )
                
                if not chunks:
                    print(f"    [WARNING] No valid chunks")
                    continue
                
                # Content-addressed IDs remain stable when a newly generated
                # note sorts before existing notes or PDF order changes.
                file_hash = get_file_hash(pdf_path)
                ids = build_chunk_ids(group_hash, file_hash, len(chunks))
                metas = [{
                    "pdf_path": pdf_path,
                    "pdf_filename": filename,
                    "paper_group": group_idx,      # 组编号（1-6，对应6篇笔记）
                    "file_index": file_idx,        # 组内文件序号（0=主文）
                    "chunk_index": k,
                    "is_main": is_main,            # 是否主文
                    "is_si": not is_main,          # 是否SI
                    "group_hash": group_hash[:16], # 组hash前缀
                    "file_hash": file_hash,
                    "id_schema": PAPERS_ID_SCHEMA,
                    "zotero_parent_key": parent_key or ""  # Zotero父条目key
                } for k in range(len(chunks))]

                # Stale-chunk cleanup: when a paper's content changes, its
                # group_hash changes; without this delete the old chunks
                # would linger as duplicate semantic content. We key on
                # pdf_path because that's the stable identity across content
                # revisions. ChromaDB 1.5.5 (Rust backend) returns silently
                # on a 0-match delete, so no try/except is needed.
                col.delete(where={"pdf_path": pdf_path})

                col.add(documents=chunks, ids=ids, metadatas=metas)
                print(f"    [OK] {len(chunks)} chunks")
                group_chunks_count += len(chunks)
                
            except KeyboardInterrupt:
                print(f"\n[INTERRUPTED] Group {group_idx} partially processed.")
                print("Run again to continue from this group.")
                return
            except Exception as e:
                print(f"    [ERROR] {e}")
                group_had_errors = True

        # 本组全部处理完成才写入 ledger。任何一个文件抽取失败都不记账，
        # 否则失败的那个 PDF 永远不会被重试（组 hash 已被标记为完成）。
        if group_had_errors:
            print(f"  [WARNING] Group had file errors; NOT recording in ledger (will retry next run)")
        elif group_chunks_count > 0:
            with open(LEDGER_PATH, 'a', encoding='utf-8') as f:
                f.write(group_hash + '\n')
            processed.add(group_hash)
            print(f"  [GROUP DONE] Total {group_chunks_count} chunks")
        else:
            print(f"  [WARNING] No chunks extracted from this group")

    print(f"\n[SUMMARY] Total chunks in collection: {col.count()}")

if __name__ == "__main__":
    main()
