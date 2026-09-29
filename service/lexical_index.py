"""Keyword (BM25) sidecar for one canonical papers generation.

The sidecar is a derived, disposable SQLite FTS5 file stored inside the
generation directory. It only proposes chunk IDs: every returned passage is
re-read from the pinned Chroma collection and re-verified against canonical
page coordinates by the caller, so a stale or damaged sidecar can reduce recall
but cannot inject unverified text.
"""
from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

SCHEMA_VERSION = "lexical-fts5-v1"
TOKENIZER = "unicode61 remove_diacritics 2"
SIDECAR_RELATIVE = Path("derived") / "lexical-v1.sqlite"
FILTER_FIELDS = ("zotero_parent_key", "zotero_attachment_key", "source_role", "pdf_filename")
MAX_QUERY_TERMS = 32
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
# Only extremely common English function words; BM25 IDF handles the rest.
_STOPWORDS = frozenset("""
a an and are as at be by for from has have in is it its of on or that the this
to was were what which with how does do did can between than these those their
""".split())


class LexicalIndexError(ValueError):
    pass


def fts5_available() -> bool:
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(text)")
        return True
    except sqlite3.OperationalError:
        return False


def sidecar_path(reader) -> Path:
    return Path(reader.directory) / SIDECAR_RELATIVE


def query_terms(text: str) -> list[str]:
    """Tokenize like FTS5 unicode61: letters and digits, case-folded."""
    terms, seen = [], set()
    for token in _TOKEN_RE.findall(text or ""):
        term = token.casefold()
        if term in _STOPWORDS or term in seen:
            continue
        seen.add(term)
        terms.append(term)
        if len(terms) == MAX_QUERY_TERMS:
            break
    return terms


def _match_expression(terms: list[str]) -> str:
    # Quoted terms are literal FTS5 strings; the tokenizer never emits quotes.
    return " OR ".join('"' + term + '"' for term in terms)


def build_sidecar(collection, reader, *, batch_size: int = 2000, output: Path | None = None) -> dict:
    """Write the sidecar for the reader's generation from its Chroma documents."""
    if not fts5_available():
        raise LexicalIndexError("This Python's sqlite3 lacks FTS5; keyword search is unavailable")
    manifest = reader.manifest
    generation_id = manifest["generation_id"]
    if (collection.metadata or {}).get("generation_id") != generation_id:
        raise LexicalIndexError("Collection belongs to a different generation")
    expected = manifest["item_count"]
    target = Path(output) if output else sidecar_path(reader)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + f".tmp-{os.getpid()}")
    if temporary.exists():
        temporary.unlink()
    columns = ", ".join(f"{field} UNINDEXED" for field in FILTER_FIELDS)
    placeholders = ", ".join("?" for _ in range(2 + len(FILTER_FIELDS)))
    identities = []  # (chunk_id, text_hash) only; text is streamed into SQLite
    try:
        with closing(sqlite3.connect(temporary)) as connection:
            connection.execute(
                f"CREATE VIRTUAL TABLE chunks USING fts5(chunk_id UNINDEXED, text, {columns}, "
                f"tokenize='{TOKENIZER}')"
            )
            connection.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            offset = 0
            while True:
                page = collection.get(limit=batch_size, offset=offset, include=["documents", "metadatas"])
                ids = page["ids"]
                if not ids:
                    break
                rows = []
                for chunk_id, document, metadata in zip(ids, page["documents"], page["metadatas"]):
                    metadata = metadata or {}
                    if metadata.get("generation_id") != generation_id:
                        raise LexicalIndexError("Chunk belongs to a different generation: " + chunk_id)
                    text_hash = hashlib.sha256((document or "").encode("utf-8")).hexdigest()
                    if text_hash != metadata.get("text_hash"):
                        raise LexicalIndexError("Chunk text hash mismatch: " + chunk_id)
                    identities.append((chunk_id, text_hash))
                    rows.append((chunk_id, document,
                                 *(str(metadata.get(field) or "") for field in FILTER_FIELDS)))
                connection.executemany(f"INSERT INTO chunks VALUES ({placeholders})", rows)
                offset += len(ids)
            if len(identities) != expected or len({i for i, _ in identities}) != expected:
                raise LexicalIndexError(
                    f"Collection returned {len(identities)} chunks; generation declares {expected}")
            digest = hashlib.sha256()
            for chunk_id, text_hash in sorted(identities):
                digest.update(chunk_id.encode("utf-8") + b"\0" + bytes.fromhex(text_hash))
            meta = {
                "schema_version": SCHEMA_VERSION,
                "tokenizer": TOKENIZER,
                "generation_id": generation_id,
                "collection_name": manifest["collection_name"],
                "item_count": str(expected),
                "content_digest": digest.hexdigest(),
            }
            connection.executemany("INSERT INTO meta VALUES (?, ?)", meta.items())
            connection.commit()
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return {"path": str(target), "generation_id": generation_id, "item_count": expected,
            "content_digest": meta["content_digest"]}


class LexicalIndex:
    """Read-only keyword index bound to exactly one generation."""

    def __init__(self, path: Path, manifest: dict):
        self.path = Path(path)
        if not self.path.is_file():
            raise LexicalIndexError("Keyword index has not been built for the active generation")
        uri = self.path.resolve().as_uri() + "?mode=ro"
        # One read-only connection shared by server threads; queries are serialized.
        self._lock = threading.Lock()
        self._connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        try:
            meta = dict(self._connection.execute("SELECT key, value FROM meta"))
        except sqlite3.DatabaseError as exc:
            self._connection.close()
            raise LexicalIndexError(f"Keyword index is unreadable: {exc}") from exc
        expected = {
            "schema_version": SCHEMA_VERSION,
            "tokenizer": TOKENIZER,
            "generation_id": manifest["generation_id"],
            "collection_name": manifest["collection_name"],
            "item_count": str(manifest["item_count"]),
        }
        for key, value in expected.items():
            if meta.get(key) != value:
                self._connection.close()
                raise LexicalIndexError(f"Keyword index {key} does not match the active generation")
        self.meta = meta

    @classmethod
    def open_for(cls, reader) -> "LexicalIndex":
        return cls(sidecar_path(reader), reader.manifest)

    def status(self) -> dict:
        return {"ready": True, "schema_version": self.meta["schema_version"],
                "generation_id": self.meta["generation_id"],
                "item_count": int(self.meta["item_count"]),
                "content_digest": self.meta["content_digest"]}

    def search(self, query: str, limit: int, **filters) -> list[tuple[str, float]]:
        """Return (chunk_id, bm25) pairs, best first; all filters are ANDed."""
        terms = query_terms(query)
        if not terms or limit < 1:
            return []
        clauses, parameters = ["chunks MATCH ?"], [_match_expression(terms)]
        for field in FILTER_FIELDS:
            value = filters.get(field)
            if value:
                clauses.append(f"{field} = ?")
                parameters.append(value)
        parameters.append(limit)
        sql = (f"SELECT chunk_id, bm25(chunks) AS score FROM chunks WHERE {' AND '.join(clauses)} "
               "ORDER BY score, chunk_id LIMIT ?")
        with self._lock:
            return [(row[0], float(row[1])) for row in self._connection.execute(sql, parameters).fetchall()]

    def close(self) -> None:
        self._connection.close()


def reciprocal_rank_fusion(rankings: dict[str, list[str]], *, k: int = 60) -> list[tuple[str, dict]]:
    """Fuse ranked ID lists; ties keep the first list's order (dense first)."""
    fused: dict[str, dict] = {}
    for source_index, (name, ids) in enumerate(rankings.items()):
        for rank, identifier in enumerate(ids, 1):
            entry = fused.setdefault(identifier, {"score": 0.0, "first": (source_index, rank)})
            entry["score"] += 1.0 / (k + rank)
            entry[name + "_rank"] = rank
    ordered = sorted(fused.items(), key=lambda item: (-item[1]["score"], item[1]["first"]))
    return [(identifier, {key: value for key, value in entry.items() if key != "first"})
            for identifier, entry in ordered]
