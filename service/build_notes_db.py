"""Build an immutable, full-snapshot notes index generation."""

from __future__ import annotations

import argparse
import hashlib
import math
import re
import sys
from pathlib import Path
from typing import Callable, Mapping, Sequence

import chromadb
import yaml

from config import (
    CHROMA_PATH,
    NOTES_COLLECTION_NAME as COLLECTION_NAME,
    NOTES_DIR,
    NOTES_LEDGER as LEDGER_PATH,
    NOTE_SUFFIX,
)
from embedding_client import (
    embed_index_text,
    embedding_contract,
    get_embedding,  # noqa: F401 - retained as a legacy re-export
    split_embedding_text,
)
from index_generation import GenerationStore, PublicationCommittedError, implementation_contract


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_HEADING_RE = re.compile(r"(?m)^#{1,6}[ \t]+(.+?)[ \t]*$")
_PIPELINE_SCHEMA = "notes-heading-sections-v1"


class NotesBuildError(RuntimeError):
    """Raised when a candidate notes generation cannot be published."""


def parse_frontmatter(text):
    """Parse YAML frontmatter and return ``(metadata, body)``."""

    match = _FRONTMATTER_RE.match(text)
    if match:
        try:
            metadata = yaml.safe_load(match.group(1)) or {}
        except yaml.YAMLError:
            metadata = {}
        return metadata, text[match.end() :]
    return {}, text


def _file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def note_document_id(filename):
    """Stable legacy-compatible ID derived from one note filename."""

    return hashlib.md5(filename.encode("utf-8")).hexdigest()


def plan_note_ingest(all_notes, processed, existing_ids, notes_dir):
    """Legacy pure planner retained for compatibility with existing callers."""

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
    """Load the legacy append-only ledger without using it for new builds."""

    entries = {}
    if LEDGER_PATH.exists():
        with LEDGER_PATH.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                name, separator, digest = line.partition("\t")
                entries[name] = digest if separator else ""
    return entries


def append_ledger(filename, content_hash=""):
    """Legacy helper retained for compatibility; generation builds do not call it."""

    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"{filename}\t{content_hash}\n")


def _validated_parent_key(metadata: Mapping[str, object], source_file: str) -> str:
    value = metadata.get("zotero_parent_key")
    if not isinstance(value, str) or not value.strip():
        raise NotesBuildError(
            f"{source_file}: non-empty zotero_parent_key is required"
        )
    return value.strip()


def _scan_notes(notes_dir: Path, note_suffix: str):
    notes_dir = Path(notes_dir)
    if not notes_dir.is_dir():
        raise NotesBuildError(f"NOTES_DIR does not exist: {notes_dir}")
    try:
        paths = sorted(
            path
            for path in notes_dir.iterdir()
            if path.is_file() and path.name.endswith(note_suffix)
        )
    except OSError as exc:
        raise NotesBuildError(f"Cannot scan NOTES_DIR: {notes_dir}") from exc
    if not paths:
        raise NotesBuildError("No note files found; refusing an empty snapshot")

    notes = []
    for path in paths:
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise NotesBuildError(f"Cannot read UTF-8 note: {path.name}") from exc
        metadata, _body = parse_frontmatter(text)
        if not isinstance(metadata, Mapping):
            raise NotesBuildError(f"{path.name}: frontmatter must be an object")
        notes.append(
            {
                "note_id": note_document_id(path.name),
                "path": path.resolve(),
                "raw": raw,
                "text": text,
                "zotero_parent_key": _validated_parent_key(metadata, path.name),
                "content_hash": hashlib.sha256(raw).hexdigest(),
            }
        )
    return notes


def _section_ranges(text: str):
    """Return contiguous heading-based ranges covering the complete note."""

    frontmatter = _FRONTMATTER_RE.match(text)
    body_start = frontmatter.end() if frontmatter else 0
    headings = list(_HEADING_RE.finditer(text, body_start))
    if not headings:
        return ((0, len(text), "document"),)

    ranges = []
    if headings[0].start() > 0:
        ranges.append((0, headings[0].start(), "frontmatter"))
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        ranges.append((heading.start(), end, heading.group(1).strip()))
    return tuple(ranges)


def _validated_splits(
    section_text: str,
    split_fn: Callable[[str], Sequence[tuple[int, int, str]]],
):
    pieces = tuple(split_fn(section_text))
    if not pieces:
        raise NotesBuildError("Embedding splitter returned no text")
    cursor = 0
    for piece in pieces:
        if not isinstance(piece, (tuple, list)) or len(piece) != 3:
            raise NotesBuildError("Embedding splitter returned an invalid record")
        start, end, text = piece
        if (
            not isinstance(start, int)
            or not isinstance(end, int)
            or not isinstance(text, str)
            or start != cursor
            or end <= start
            or section_text[start:end] != text
        ):
            raise NotesBuildError(
                "Embedding splitter did not preserve contiguous source coverage"
            )
        cursor = end
    if cursor != len(section_text):
        raise NotesBuildError(
            "Embedding splitter did not preserve contiguous source coverage"
        )
    return pieces


def _sources(notes):
    return [
        {
            "source_id": note["note_id"],
            "content_hash": note["content_hash"],
            "status": "success",
            "path": str(note["path"]),
        }
        for note in notes
    ]


def _pipeline_contract(note_suffix: str, embedding):
    return {
        "embedding": embedding,
        "pipeline": implementation_contract(
            [Path(__file__), Path(__file__).with_name("index_generation.py")],
            {
                "schema": _PIPELINE_SCHEMA,
                "note_suffix": note_suffix,
                "identity": "filename-md5-v1",
                "artifact": "verbatim-utf8-note-v1",
                "distance": "cosine",
            },
        ),
    }


def _collection_count(client, collection_name: str) -> int:
    try:
        return int(client.get_collection(collection_name).count())
    except Exception as exc:
        raise NotesBuildError(
            f"Cannot verify collection {collection_name}"
        ) from exc


def build_notes_generation(
    *,
    notes_dir: Path = NOTES_DIR,
    chroma_path: Path = CHROMA_PATH,
    collection_name: str = COLLECTION_NAME,
    note_suffix: str = NOTE_SUFFIX,
    allow_removals: bool = False,
    rebuild: bool = False,
    client_factory=chromadb.PersistentClient,
    embed_fn: Callable[[str], Sequence[float]] = embed_index_text,
    split_fn: Callable[[str], Sequence[tuple[int, int, str]]] = split_embedding_text,
    embedding_contract_fn: Callable[[], Mapping[str, object]] = embedding_contract,
):
    """Build and atomically publish one complete notes generation."""

    store = GenerationStore(Path(chroma_path), collection_name)
    sources = []
    contract = {
        "preflight": {"schema": _PIPELINE_SCHEMA, "note_suffix": note_suffix}
    }
    generation = None

    with store.writer_lock():
        try:
            notes = _scan_notes(Path(notes_dir), note_suffix)
            sources = _sources(notes)
            contract = _pipeline_contract(
                note_suffix, dict(embedding_contract_fn())
            )
            embedding_metadata = contract["embedding"]
            dimensions = embedding_metadata.get("dimensions")
            if (
                not isinstance(dimensions, int)
                or isinstance(dimensions, bool)
                or dimensions <= 0
            ):
                raise NotesBuildError(
                    "Embedding contract requires positive dimensions"
                )
            provider = embedding_metadata.get("provider")
            model = embedding_metadata.get("model")
            if not isinstance(provider, str) or not provider.strip():
                raise NotesBuildError("Embedding contract requires a provider")
            if not isinstance(model, str) or not model.strip():
                raise NotesBuildError("Embedding contract requires a model")

            client = client_factory(path=str(chroma_path))
            active = store.load_active()
            if (not rebuild and store.same_inputs(active, contract, sources)
                    and store.generation_usable(client, active)):
                return active, True

            generation = store.begin(contract, sources)
            generation_dir = store.generation_path(generation)
            notes_artifact_dir = generation_dir / "notes"
            notes_artifact_dir.mkdir(parents=True, exist_ok=False)
            collection = client.get_or_create_collection(
                name=generation["collection_name"],
                metadata={
                    "hnsw:space": "cosine",
                    "embed_provider": provider,
                    "embed_model": model,
                    "generation_id": generation["generation_id"],
                },
            )

            artifacts = {"notes": {}}
            item_count = 0
            for note in notes:
                artifact_relative = f"notes/{note['note_id']}.md"
                artifact_path = generation_dir / artifact_relative
                artifact_path.write_bytes(note["raw"])
                artifacts["notes"][note["note_id"]] = artifact_relative

                for section_start, section_end, title in _section_ranges(note["text"]):
                    section_text = note["text"][section_start:section_end]
                    for local_start, local_end, chunk_text in _validated_splits(
                        section_text, split_fn
                    ):
                        start = section_start + local_start
                        end = section_start + local_end
                        embedding = [float(value) for value in embed_fn(chunk_text)]
                        if (
                            len(embedding) != dimensions
                            or not all(math.isfinite(value) for value in embedding)
                            or not any(embedding)
                        ):
                            raise NotesBuildError(
                                "Embedding does not match the declared dimensions"
                            )
                        record_id = f"{note['note_id']}:{start}:{end}"
                        collection.upsert(
                            ids=[record_id],
                            documents=[chunk_text],
                            embeddings=[embedding],
                            metadatas=[
                                {
                                    "note_id": note["note_id"],
                                    "source_file": note["path"].name,
                                    "zotero_parent_key": note["zotero_parent_key"],
                                    "start": start,
                                    "end": end,
                                    "section_title": title,
                                    "generation_id": generation["generation_id"],
                                    "embedding_truncated": False,
                                }
                            ],
                        )
                        item_count += 1

            actual_count = int(collection.count())
            if item_count <= 0 or actual_count != item_count:
                raise NotesBuildError(
                    f"Candidate collection count mismatch: {actual_count} != {item_count}"
                )
            published = store.publish(
                generation,
                item_count=item_count,
                artifacts=artifacts,
                allow_removals=allow_removals,
            )
            return published, False
        except BaseException as exc:
            try:
                if generation is None:
                    generation = store.begin(contract, sources)
                outcome = store.fail(generation, exc)
            except Exception:
                outcome = "unknown"  # Diagnostics must not replace the original exception.
            if outcome == "committed" and not isinstance(exc, PublicationCommittedError):
                raise PublicationCommittedError(generation["generation_id"], "notes build", exc) from exc
            raise


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-removals",
        action="store_true",
        help="Allow sources absent from the full current snapshot to be withdrawn.",
    )
    parser.add_argument("--rebuild", action="store_true",
                        help="Force a fresh notes candidate without deleting active data.")
    args = parser.parse_args(argv)
    print("[INIT] Building immutable notes index generation...")
    print(f"  Notes dir:  {NOTES_DIR}")
    print(f"  ChromaDB:   {CHROMA_PATH}")
    print(f"  Collection: {COLLECTION_NAME}")
    try:
        options = {"allow_removals": args.allow_removals}
        if args.rebuild:
            options["rebuild"] = True
        manifest, reused = build_notes_generation(**options)
    except PublicationCommittedError as exc:
        print(f"[COMMITTED WITH WARNING] {exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("[INTERRUPTED] Inspect index status before retrying; publication outcome may be unknown.")
        return 130
    except Exception as exc:
        print(f"[FATAL] Notes index build failed: {exc}", file=sys.stderr)
        return 1
    action = "Reused" if reused else "Published"
    try:
        print(
            f"[DONE] {action} generation {manifest['generation_id']} "
            f"({manifest['item_count']} sections)."
        )
    except (OSError, KeyboardInterrupt):
        return 3  # The output stream failed, not the already committed build.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
