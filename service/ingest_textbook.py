"""
ingest_textbook.py — Large-textbook batched ingester.

Usage:
  python ingest_textbook.py --pdf-path /path/to/book.pdf --zotero-key ABCDEFGH

Differences from build_pdf_db.py:
  - Submits chunks in batches of TEXTBOOK_BATCH_SIZE to avoid ChromaDB timeouts
  - Designed for textbooks (200+ pages)
  - Uses an independent ledger (LOCALRAG_TEXTBOOK_LEDGER)

All paths and tunables come from environment variables — see config.py.
"""
import argparse
import hashlib
import os
import re
import sys

import chromadb
import pdfplumber

from config import (
    CHROMA_PATH,
    PAPERS_COLLECTION_NAME as COLLECTION,
    TEXTBOOK_LEDGER as LEDGER_PATH,
    CHUNK_SIZE,
    CHUNK_STEP,
    MIN_CHUNK_LEN,
    TEXTBOOK_BATCH_SIZE as BATCH_SIZE,
)
from embedding_client import get_chromadb_embedding_function

REF_PATTERN = re.compile(
    r'\n(References|REFERENCES|Bibliography|BIBLIOGRAPHY|参考文献|Acknowledgements|ACKNOWLEDGEMENTS)\s*\n',
    re.IGNORECASE
)


def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(8192), b""):
            h.update(block)
    return h.hexdigest()


def extract_text(pdf_path):
    with pdfplumber.open(pdf_path) as pdf:
        total = len(pdf.pages)
        print(f"  Pages: {total}")
        full_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    match = None
    for m in REF_PATTERN.finditer(full_text):
        match = m
    if match:
        full_text = full_text[:match.start()]
    return full_text


def chunk_text(text):
    chunks = []
    for i in range(0, len(text), CHUNK_STEP):
        chunk = text[i:i + CHUNK_SIZE]
        if len(chunk) > MIN_CHUNK_LEN:
            chunks.append(chunk)
    return chunks


def load_ledger():
    if LEDGER_PATH.exists():
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            return set(line.strip() for line in f)
    return set()


def write_ledger(entry):
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write(entry + "\n")


def main():
    parser = argparse.ArgumentParser(description="Ingest large textbook PDF into ChromaDB")
    parser.add_argument("--pdf-path",     required=True, help="Path to PDF file")
    parser.add_argument("--zotero-key",   required=True, help="Zotero parent item key (e.g. U7JGGZUH)")
    parser.add_argument("--dry-run",      action="store_true", help="Only extract and chunk, do not write to DB")
    args = parser.parse_args()

    pdf_path = args.pdf_path
    zotero_key = args.zotero_key

    if not os.path.exists(pdf_path):
        print(f"[ERROR] File not found: {pdf_path}")
        sys.exit(1)

    # Check ledger
    processed = load_ledger()
    fhash = file_hash(pdf_path)
    if fhash in processed:
        print(f"[SKIP] Already ingested: {os.path.basename(pdf_path)} (hash {fhash[:16]})")
        return

    print(f"[START] {os.path.basename(pdf_path)}")
    print(f"  Zotero key: {zotero_key}")

    # Extract text
    print("[1/3] Extracting text...")
    full_text = extract_text(pdf_path)
    print(f"  Text length: {len(full_text):,} chars")

    if not full_text.strip():
        print("[ERROR] No text extracted — is this a scanned PDF?")
        sys.exit(1)

    # Chunk
    print("[2/3] Chunking...")
    chunks = chunk_text(full_text)
    print(f"  Total chunks: {len(chunks)}")

    if args.dry_run:
        print("[DRY RUN] Skipping DB write.")
        return

    # Connect to ChromaDB. Use the provider abstraction — hardcoding
    # OllamaEmbeddingFunction here bypassed LOCALRAG_EMBED_PROVIDER and
    # would embed textbooks with a different model than the rest of the
    # papers collection.
    ef = get_chromadb_embedding_function()
    CHROMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    col = client.get_or_create_collection(name=COLLECTION, embedding_function=ef)

    filename = os.path.basename(pdf_path)
    short_hash = fhash[:16]

    # Build IDs and metadatas
    ids   = [f"textbook_{short_hash}_chunk_{k}" for k in range(len(chunks))]
    metas = [{
        "pdf_path":          pdf_path,
        "pdf_filename":      filename,
        "paper_group":       -1,        # -1 = ingested via ingest_textbook.py
        "file_index":        0,
        "chunk_index":       k,
        "is_main":           True,
        "is_si":             False,
        "group_hash":        short_hash,
        "zotero_parent_key": zotero_key,
    } for k in range(len(chunks))]

    # Batch add
    print(f"[3/3] Writing to ChromaDB in batches of {BATCH_SIZE}...")
    total_written = 0
    for start in range(0, len(chunks), BATCH_SIZE):
        end = min(start + BATCH_SIZE, len(chunks))
        col.add(
            documents=chunks[start:end],
            ids=ids[start:end],
            metadatas=metas[start:end],
        )
        total_written += end - start
        print(f"  [{total_written}/{len(chunks)}] chunks written", end="\r")

    print(f"\n[DONE] {total_written} chunks ingested.")
    print(f"  Total in collection: {col.count()}")

    write_ledger(fhash)
    print(f"  Ledger updated: {LEDGER_PATH}")


if __name__ == "__main__":
    main()
