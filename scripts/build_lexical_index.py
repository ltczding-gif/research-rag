#!/usr/bin/env python3
"""Build the keyword (BM25) index for the active canonical papers generation.

The index is a derived SQLite FTS5 file inside the generation directory. It is
needed only for retrieval_mode=hybrid/lexical; building it changes nothing
while the server default stays dense. Rebuild after every new papers
generation (scripts/build_indexes.py does this automatically).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SERVICE_DIR = REPO_ROOT / "service"
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))


def build(chroma_path, logical_name, *, client=None):
    import chromadb
    from generation_query import GenerationReader
    from index_generation import GenerationStore
    from lexical_index import build_sidecar

    store = GenerationStore(chroma_path, logical_name)
    manifest = store.load_active()
    if manifest is None:
        raise SystemExit(f"No active canonical '{logical_name}' generation; build the papers index first.")
    store.validate_artifacts(manifest)
    client = client or chromadb.PersistentClient(path=str(chroma_path))
    collection = client.get_collection(manifest["collection_name"], embedding_function=None)
    if collection.count() != manifest["item_count"]:
        raise SystemExit("Active collection count differs from its manifest; inspect index status first.")
    return build_sidecar(collection, GenerationReader(store, manifest))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    from config import CHROMA_PATH, PAPERS_COLLECTION_NAME

    result = build(CHROMA_PATH, PAPERS_COLLECTION_NAME)
    print(json.dumps(result, indent=2))
    print("[OK] Keyword index ready. Restart MCP/query processes to load it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
