"""
Build Notes Vector DB

Ingest review notes from LOCALRAG_NOTES_DIR into the ChromaDB `notes` collection.
Each note is stored as a single record (no chunking); metadata is taken from
the note's YAML frontmatter.

Configure via environment variables — see service/config.py and .env.example.
"""

import os
import re
import sys
import yaml
import hashlib
import chromadb
import json
import urllib.request
from pathlib import Path

from config import (
    NOTES_DIR,
    CHROMA_PATH,
    NOTES_COLLECTION_NAME as COLLECTION_NAME,
    NOTES_LEDGER as LEDGER_PATH,
    NOTE_SUFFIX,
)
from config import EMBED_PROVIDER
from embedding_client import active_model_id, get_embedding  # noqa: F401  (re-exported)


def parse_frontmatter(text):
    """解析 YAML frontmatter，返回 (metadata_dict, body_text)"""
    m = re.match(r'^---\s*\n(.*?)\n---\s*\n', text, re.DOTALL)
    if m:
        try:
            fm = yaml.safe_load(m.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        body = text[m.end():]
        return fm, body
    return {}, text


def _file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def note_document_id(filename):
    """Stable ID for one note filename (content changes are handled by upsert)."""
    return hashlib.md5(filename.encode("utf-8")).hexdigest()


def plan_note_ingest(all_notes, processed, existing_ids, notes_dir):
    """Return (files_to_process, legacy_ledger_upgrades).

    A ledger hit is not enough to skip: the corresponding record must still
    exist in Chroma. This self-heals deleted/rebuilt collections and partial
    legacy migrations instead of permanently hiding missing notes.
    """
    to_process = []
    upgrades = []
    for filename in all_notes:
        recorded = processed.get(filename)
        current_hash = _file_sha256(Path(notes_dir) / filename)
        if recorded is None or note_document_id(filename) not in existing_ids:
            to_process.append(filename)
        elif recorded == "":
            upgrades.append((filename, current_hash))
        elif recorded != current_hash:
            to_process.append(filename)
    return to_process, upgrades


def load_ledger():
    """加载已处理笔记列表 -> {filename: content_hash}

    新格式每行 "filename\\thash"，旧格式只有 filename（hash 记为 ""）。
    重复行后写覆盖先写（last-wins），所以更新时直接追加新行即可。
    """
    entries = {}
    if LEDGER_PATH.exists():
        with open(LEDGER_PATH, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                name, sep, digest = line.partition("\t")
                entries[name] = digest if sep else ""
    return entries


def append_ledger(filename, content_hash=""):
    """追加一条到 ledger（含内容 hash，编辑过的笔记才能被重新入库）"""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write(f"{filename}\t{content_hash}\n")


def main():
    print("[INIT] Building notes vector DB...")
    print(f"  Notes dir:  {NOTES_DIR}")
    print(f"  ChromaDB:   {CHROMA_PATH}")
    print(f"  Collection: {COLLECTION_NAME}")
    print(f"  Ledger:     {LEDGER_PATH}")

    # 检查嵌入服务（provider-neutral：默认 fastembed，无 daemon）
    try:
        test_emb = get_embedding("test")
        dim = len(test_emb)
        print(f"  Embedding OK ({EMBED_PROVIDER}:{active_model_id()}), dim = {dim}")
    except Exception as e:
        print(f"[FATAL] Embedding provider '{EMBED_PROVIDER}' not available: {e}")
        sys.exit(1)

    # 扫描笔记文件
    if not NOTES_DIR.exists():
        print(f"[FATAL] NOTES_DIR does not exist: {NOTES_DIR}")
        sys.exit(1)
    all_notes = sorted(
        f for f in os.listdir(NOTES_DIR)
        if f.endswith(NOTE_SUFFIX) and (NOTES_DIR / f).is_file()
    )
    print(f"  Found {len(all_notes)} note files")

    # Initialize Chroma before consulting the ledger. Ledger entries are only
    # valid skips when the matching collection record still exists.
    CHROMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    col = client.get_or_create_collection(
        name=COLLECTION_NAME,
        metadata={
            "hnsw:space": "cosine",
            "embed_provider": EMBED_PROVIDER,
            "embed_model": active_model_id(),
        },
    )
    existing_ids = set(col.get(include=[]).get("ids", []))
    print(f"  Collection '{COLLECTION_NAME}' count before: {col.count()}")

    # 加载 ledger。四种情况需要处理：
    #   1. ledger 里没有 → 新笔记
    #   2. 新格式且 hash 变了 → 笔记被编辑过，重新入库（col.upsert 覆盖）
    #   3. ledger 有记录但 collection ID 丢失 → 自愈，重新入库
    #   4. 旧格式（无 hash）且 collection ID 存在 → 只升级 ledger hash
    processed = load_ledger()
    to_process, upgrades = plan_note_ingest(all_notes, processed, existing_ids, NOTES_DIR)
    for filename, content_hash in upgrades:
        append_ledger(filename, content_hash)
    print(f"  Already processed: {len(processed)}, new/changed: {len(to_process)}")

    if not to_process:
        print("[DONE] No new notes to process.")
        return

    success = 0
    failed = 0

    for i, filename in enumerate(to_process, 1):
        filepath = NOTES_DIR / filename
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                text = f.read()

            fm, body = parse_frontmatter(text)

            # 必须有 zotero_parent_key
            zpk = fm.get("zotero_parent_key", "")
            if not zpk:
                print(f"  [{i}/{len(to_process)}] SKIP (no zotero_parent_key): {filename}")
                # 标记为已处理，避免重复尝试；带 hash，笔记补上 key 后会重试
                append_ledger(filename, _file_sha256(filepath))
                continue

            # 生成 embedding（用完整笔记文本，超长则截断）
            emb = get_embedding(text)

            # 构建 metadata（ChromaDB metadata 只支持 str/int/float/bool）
            metadata = {
                "source_file": filename,
                "zotero_parent_key": str(zpk),
            }
            # 可选字段
            for key in ["year", "journal", "title_en", "title_zh", "doi", "authors"]:
                val = fm.get(key)
                if val is not None:
                    if key == "authors" and isinstance(val, list):
                        metadata[key] = ", ".join(str(a) for a in val)
                    else:
                        metadata[key] = str(val)

            # Filename-derived stable ID; content changes overwrite via upsert.
            doc_id = note_document_id(filename)

            col.upsert(
                ids=[doc_id],
                documents=[text],  # 存完整笔记文本，检索时直接返回
                embeddings=[emb],
                metadatas=[metadata],
            )

            success += 1
            append_ledger(filename, _file_sha256(filepath))

            if i % 50 == 0 or i == len(to_process):
                print(f"  [{i}/{len(to_process)}] Processed {success} OK, {failed} failed")

        except Exception as e:
            failed += 1
            print(f"  [{i}/{len(to_process)}] ERROR: {filename}: {e}")

    print(f"\n[DONE] Notes DB build complete.")
    print(f"  Processed: {success} OK, {failed} failed")
    print(f"  Collection '{COLLECTION_NAME}' count after: {col.count()}")


if __name__ == "__main__":
    main()
