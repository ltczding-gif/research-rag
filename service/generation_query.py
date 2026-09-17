"""Read immutable generation artifacts and validate returned source evidence."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from index_generation import GenerationStore
from pdf_ir import locate_canonical_text


def sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _context_bounds(text, start, end, budget=3200):
    """Keep the hit intact and prefer sentence edges within a bounded window.

    Sentence edges are a display heuristic, not proof of scientific completeness.
    In particular, PDF line breaks alone must not split a condition from its value.
    """
    remaining = max(0, budget - (end - start))
    left = min(start, remaining // 2)
    right = min(len(text) - end, remaining - left)
    left = min(start, remaining - right)
    lower, upper = start - left, end + right
    edges = []
    # Include lookahead so a budget cut after "0." does not look like a true EOF.
    window = text[lower:min(len(text), upper + 1)]
    for match in re.finditer(r'''(?:[。！？]["”’')\]]*\s*|[.!?]["”’')\]]*(?:\s+|$))''', window):
        if lower + match.end() > upper:
            continue
        punctuation = lower + match.start()
        if re.search(r"\b(?:vs|figs?|eqs?|et al|e\.g|i\.e)\.$",
                     text[max(0, punctuation - 8):punctuation + 1], re.IGNORECASE):
            continue
        edges.append(lower + match.end())
    starts = [edge for edge in edges if edge <= start]
    ends = [edge for edge in edges if edge >= end]
    if lower == 0:
        start_status = "source_start"
    elif starts:
        lower, start_status = starts[0], "sentence_heuristic"
    else:
        start_status = "budget_cut"
    if upper == len(text):
        end_status = "source_end"
    elif ends:
        upper, end_status = ends[-1], "sentence_heuristic"
    else:
        end_status = "budget_cut"
    return lower, upper, {"start": start_status, "end": end_status}


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

    def context(self, metadata, content, chunk_id):
        """Return one source-coordinate window, without overlapping chunk copies.

        Text, units and page breaks come verbatim from the pinned canonical PDF.
        The expanded source has its own spans; it is not an indexed chunk.
        """
        self.evidence(metadata, content, chunk_id)
        pages = sorted(
            (page for (file_id, _), page in self._pages.items()
             if file_id == metadata["file_id"]),
            key=lambda page: page["pdf_page_index"],
        )
        spans = json.loads(metadata["source_spans_json"])
        start = locate_canonical_text(pages, content, spans)
        text = "\n".join(page["normalized_text"] for page in pages)
        lower, upper, boundaries = _context_bounds(text, start, start + len(content))
        expanded = text[lower:upper]
        expanded_spans, cursor = [], 0
        for page in pages:
            page_end = cursor + len(page["normalized_text"])
            left, right = max(lower, cursor), min(upper, page_end)
            if left < right:
                expanded_spans.append({
                    "file_id": metadata["file_id"], "pdf_page_index": page["pdf_page_index"],
                    "char_start_in_normalized_page": left - cursor,
                    "char_end_in_normalized_page": right - cursor,
                    "page_text_hash": page["page_text_hash"],
                })
            cursor = page_end + 1
        source = self.evidence(
            {**metadata, "text_hash": sha256(expanded),
             "source_spans_json": json.dumps(expanded_spans)}, expanded, chunk_id,
        )
        source.pop("chunk_id")
        match_start, match_end = start - lower, start - lower + len(content)
        source.update(for_chunk_id=chunk_id, text_hash=sha256(expanded),
                      match_start=match_start, match_end=match_end,
                      boundary_status=boundaries, text_format="verbatim_canonical_pdf_text",
                      budget_codepoints=3200, oversized_match=len(content) > 3200)
        marked = (expanded[:match_start] + "[MATCH]" + content + "[/MATCH]"
                  + expanded[match_end:])
        return {"context": marked, "context_source": source}


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
