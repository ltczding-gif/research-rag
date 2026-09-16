"""Read immutable generation artifacts and validate returned source evidence."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from index_generation import GenerationStore
from pdf_ir import locate_canonical_text


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class GenerationReader:
    def __init__(self, store, manifest):
        self.store = store
        self.manifest = manifest
        self.directory = store.generation_path(manifest)
        self._pages = None

    def artifact(self, relative):
        path = (self.directory / relative).resolve()
        if not path.is_relative_to(self.directory.resolve()):
            raise ValueError("Generation artifact escapes its directory")
        return path

    def check_embedding(self, contract_fn):
        expected = self.manifest["contract"]["embedding"]
        actual = contract_fn(dimensions=expected["dimensions"])
        if actual != expected:
            raise ValueError("Embedding contract changed; build a new candidate generation")

    def full_note(self, metadata):
        self.check_metadata(metadata)
        note_id = metadata["note_id"]
        relative = self.manifest["artifacts"]["notes"][note_id]
        raw = self.artifact(relative).read_bytes()
        source = next(item for item in self.manifest["sources"] if item["source_id"] == note_id)
        if hashlib.sha256(raw).hexdigest() != source["content_hash"]:
            raise ValueError("Stored note hash does not match its generation")
        return raw.decode("utf-8")

    def check_metadata(self, metadata):
        if metadata.get("generation_id") != self.manifest["generation_id"]:
            raise ValueError("Result belongs to a different index generation")

    def evidence(self, metadata, content, chunk_id):
        self.check_metadata(metadata)
        if sha256(content) != metadata["text_hash"]:
            raise ValueError("Indexed chunk text hash mismatch")
        if self._pages is None:
            path = self.artifact(self.manifest["artifacts"]["pages"])
            pages = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
            self._pages = {(page["file_id"], page["pdf_page_index"]): page for page in pages}
            if len(self._pages) != len(pages):
                raise ValueError("Duplicate canonical page identity")
        segments = []
        spans = json.loads(metadata["source_spans_json"])
        if not spans:
            raise ValueError("Canonical chunk has no source spans")
        for span in spans:
            page = self._pages[(span["file_id"], span["pdf_page_index"])]
            for field in ("generation_id", "paper_id", "file_id", "file_hash", "extractor_fingerprint",
                          "zotero_parent_key", "zotero_attachment_key", "source_role"):
                if page[field] != metadata[field]:
                    raise ValueError("Canonical page identity mismatch: " + field)
            text = page["normalized_text"]
            if sha256(text) != page["page_text_hash"] or page["page_text_hash"] != span["page_text_hash"]:
                raise ValueError("Canonical page text hash mismatch")
            start, end = span["char_start_in_normalized_page"], span["char_end_in_normalized_page"]
            if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(text):
                raise ValueError("Invalid canonical source interval")
            quote = text[start:end]
            segments.append({**span, "page_number": span["pdf_page_index"] + 1,
                             "quote": quote, "quote_hash": sha256(quote)})
        locate_canonical_text(
            [page for (file_id, _), page in self._pages.items() if file_id == metadata["file_id"]],
            content, spans,
        )
        return {"verified": True, "generation_id": self.manifest["generation_id"],
                "chunk_id": chunk_id, "file_id": metadata["file_id"],
                "file_hash": metadata["file_hash"], "zotero_parent_key": metadata["zotero_parent_key"],
                "zotero_attachment_key": metadata["zotero_attachment_key"],
                "source_role": metadata["source_role"], "segments": segments}


def open_generation(client, chroma_path, logical_name, contract_fn):
    store = GenerationStore(chroma_path, logical_name)
    manifest = store.load_active()
    if manifest is None:
        return None, None
    store.validate_artifacts(manifest)
    reader = GenerationReader(store, manifest)
    reader.check_embedding(contract_fn)
    collection = client.get_collection(manifest["collection_name"], embedding_function=None)
    if collection.count() != manifest["item_count"]:
        raise ValueError("Active collection count differs from its manifest")
    if (collection.metadata or {}).get("generation_id") != manifest["generation_id"]:
        raise ValueError("Active collection belongs to a different generation")
    return collection, reader
