"""Immutable index generations and an atomic active pointer."""
from __future__ import annotations

from contextlib import contextmanager
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
from uuid import uuid4


SCHEMA_VERSION = 1


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def atomic_write_text(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name, suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_write_json(path, value):
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def now():
    return datetime.now(timezone.utc).isoformat()


def implementation_contract(paths, settings):
    """Bind semantic settings and exact implementation bytes, excluding Git state."""
    return {"settings": settings, "implementation": {
        Path(path).name: hashlib.sha256(Path(path).read_bytes()).hexdigest()
        for path in paths
    }}


def _artifact_relative_paths(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _artifact_relative_paths(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _artifact_relative_paths(child)


def _hash_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class GenerationStore:
    def __init__(self, chroma_path, logical_name):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", logical_name):
            raise ValueError("Invalid logical collection name")
        self.logical_name = logical_name
        self.root = Path(chroma_path) / ".research-rag" / logical_name

    def generation_path(self, generation):
        identifier = generation if isinstance(generation, str) else generation["generation_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            raise ValueError("Invalid generation identity")
        return self.root / "generations" / identifier

    @contextmanager
    def writer_lock(self):
        """OS-released lock serializes builders, including after process crashes."""
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / "writer.lock").open("a+b") as handle:
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as exc:
                    raise RuntimeError("Another index builder owns this collection") from exc
            else:
                import fcntl
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    raise RuntimeError("Another index builder owns this collection") from exc
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == "nt":
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def load_active(self):
        pointer_path = self.root / "active.json"
        if not pointer_path.exists():
            return None
        pointer = read_json(pointer_path)
        manifest = read_json(self.generation_path(pointer) / "manifest.json")
        if (manifest.get("state") != "complete"
                or manifest.get("schema_version") != SCHEMA_VERSION
                or manifest.get("logical_name") != self.logical_name
                or fingerprint(manifest) != pointer.get("manifest_fingerprint")
                or manifest.get("collection_name") != pointer.get("collection_name")
                or manifest.get("generation_id") != pointer.get("generation_id")
                or manifest.get("item_count", 0) <= 0):
            raise ValueError("Active index manifest is incomplete or inconsistent")
        return manifest

    def latest_attempt(self):
        path = self.root / "latest-attempt.json"
        return read_json(path) if path.exists() else None

    def same_inputs(self, active, contract, sources):
        return bool(active and active["contract"] == contract
                    and active["sources"] == sources)

    def begin(self, contract, sources):
        identifier = uuid4().hex
        generation = {"schema_version": SCHEMA_VERSION, "generation_id": identifier,
                      "logical_name": self.logical_name,
                      "collection_name": self.logical_name + "_g_" + identifier,
                      "state": "building", "started_at": now(), "contract": contract,
                      "sources": sources, "item_count": 0, "artifacts": {}}
        self.generation_path(generation).mkdir(parents=True, exist_ok=False)
        self._save_attempt(generation)
        return generation

    def _save_attempt(self, generation):
        atomic_write_json(self.generation_path(generation) / "manifest.json", generation)
        atomic_write_json(self.root / "latest-attempt.json", {
            key: generation[key] for key in ("generation_id", "state", "started_at", "item_count")
        } | {"error": generation.get("error"), "finished_at": generation.get("finished_at")})

    def fail(self, generation, error):
        generation.update(state="failed", error=str(error), finished_at=now())
        self._save_attempt(generation)

    def _artifact_paths(self, generation, artifacts):
        generation_root = self.generation_path(generation).resolve()
        result = {}
        for relative_value in _artifact_relative_paths(artifacts):
            relative = Path(relative_value)
            if relative.is_absolute() or not relative.parts:
                raise ValueError("Generation artifact path must be relative")
            resolved = (generation_root / relative).resolve()
            if resolved == generation_root or generation_root not in resolved.parents:
                raise ValueError("Generation artifact path escapes its generation")
            result[relative.as_posix()] = resolved
        return result

    def validate_artifacts(self, manifest):
        paths = self._artifact_paths(manifest, manifest.get("artifacts", {}))
        expected = manifest.get("artifact_hashes")
        if not isinstance(expected, dict) or set(expected) != set(paths):
            raise ValueError("Generation artifact hash inventory is inconsistent")
        for relative, path in paths.items():
            if not path.is_file():
                raise ValueError(f"Generation artifact is unavailable: {relative}")
            try:
                actual_hash = _hash_file(path)
            except OSError as exc:
                raise ValueError(
                    f"Generation artifact is unreadable: {relative}"
                ) from exc
            if actual_hash != expected[relative]:
                raise ValueError(f"Generation artifact hash mismatch: {relative}")
        return True

    def publish(self, generation, *, item_count, artifacts, allow_removals=False):
        sources = generation["sources"]
        if not sources or any(source.get("status") != "success" for source in sources):
            raise ValueError("Every declared source must succeed before publication")
        if item_count <= 0:
            raise ValueError("An empty candidate cannot become active")
        artifact_paths = self._artifact_paths(generation, artifacts)
        artifact_hashes = {}
        for relative, path in artifact_paths.items():
            if not path.is_file():
                raise ValueError(f"Generation artifact is unavailable: {relative}")
            try:
                artifact_hashes[relative] = _hash_file(path)
            except OSError as exc:
                raise ValueError(
                    f"Generation artifact is unreadable: {relative}"
                ) from exc
        active = self.load_active()
        if active and not allow_removals:
            # An identical-content rename preserves source coverage. A real
            # withdrawal needs an explicit option; failed scans never reach here.
            current_ids = {source["source_id"] for source in sources}
            previous_ids = {source["source_id"] for source in active["sources"]}
            renamed_hashes = Counter(source["content_hash"] for source in sources
                                     if source["source_id"] not in previous_ids)
            removed = []
            for source in active["sources"]:
                if source["source_id"] in current_ids:
                    continue
                if renamed_hashes[source["content_hash"]] > 0:
                    renamed_hashes[source["content_hash"]] -= 1
                else:
                    removed.append(source["source_id"])
            if removed:
                raise ValueError("Source withdrawals require --allow-removals: " + ", ".join(removed))
        generation.update(
            state="complete",
            item_count=item_count,
            artifacts=artifacts,
            artifact_hashes=artifact_hashes,
            finished_at=now(),
            previous_generation=active["generation_id"] if active else None,
            previous_manifest_fingerprint=fingerprint(active) if active else None,
        )
        self._save_attempt(generation)
        atomic_write_json(self.root / "active.json", {
            "generation_id": generation["generation_id"],
            "collection_name": generation["collection_name"],
            "manifest_fingerprint": fingerprint(generation),
        })
        return generation

    @staticmethod
    def _validate_collection(client, manifest):
        try:
            collection = client.get_collection(manifest["collection_name"])
        except Exception as exc:
            raise ValueError("Generation collection is unavailable") from exc
        metadata = collection.metadata or {}
        if collection.count() != manifest["item_count"]:
            raise ValueError("Generation collection count does not match manifest")
        if metadata.get("generation_id") != manifest["generation_id"]:
            raise ValueError("Generation collection identity does not match manifest")
        return collection

    def rollback(self, client, expected_embedding_contract):
        """Atomically activate the validated previous immutable generation."""
        active = self.load_active()
        if active is None or not active.get("previous_generation"):
            raise ValueError("Active generation has no previous generation")
        expected_fingerprint = active.get("previous_manifest_fingerprint")
        if not expected_fingerprint:
            raise ValueError("Active manifest does not bind its previous manifest")
        previous_id = active["previous_generation"]
        previous = read_json(self.generation_path(previous_id) / "manifest.json")
        if (
            previous.get("state") != "complete"
            or previous.get("schema_version") != SCHEMA_VERSION
            or previous.get("logical_name") != self.logical_name
            or previous.get("generation_id") != previous_id
            or previous.get("collection_name")
            != self.logical_name + "_g_" + previous_id
            or previous.get("item_count", 0) <= 0
            or fingerprint(previous) != expected_fingerprint
        ):
            raise ValueError("Previous generation manifest is invalid")
        if previous.get("contract", {}).get("embedding") != expected_embedding_contract:
            raise ValueError("Previous generation embedding contract is incompatible")
        self.validate_artifacts(previous)
        self._validate_collection(client, previous)
        atomic_write_json(
            self.root / "active.json",
            {
                "generation_id": previous["generation_id"],
                "collection_name": previous["collection_name"],
                "manifest_fingerprint": fingerprint(previous),
            },
        )
        return previous

    def status(self, client=None):
        active = self.load_active()
        latest = self.latest_attempt()
        collection_ok = None
        collection_error = None
        artifact_ok = None
        artifact_error = None
        if active is not None and client is not None:
            try:
                self._validate_collection(client, active)
                collection_ok = True
            except ValueError as exc:
                collection_ok = False
                collection_error = str(exc)
        if active is not None:
            try:
                self.validate_artifacts(active)
                artifact_ok = True
            except ValueError as exc:
                artifact_ok = False
                artifact_error = str(exc)
        return {
            "logical_name": self.logical_name,
            "root": str(self.root.resolve()),
            "active": active,
            "active_collection_valid": collection_ok,
            "active_collection_error": collection_error,
            "active_artifacts_valid": artifact_ok,
            "active_artifacts_error": artifact_error,
            "latest_attempt": latest,
        }

    def _owned_generation_manifests(self):
        generation_root = (self.root / "generations").resolve()
        resolved_root = self.root.resolve()
        if generation_root.parent != resolved_root:
            raise ValueError("Generation root escapes logical index root")
        if not generation_root.exists():
            return {}
        manifests = {}
        for path in generation_root.iterdir():
            if not path.is_dir() or not re.fullmatch(r"[a-f0-9]{32}", path.name):
                continue
            resolved = path.resolve()
            if resolved.parent != generation_root:
                raise ValueError("Generation path escapes logical index root")
            manifest_path = resolved / "manifest.json"
            if not manifest_path.is_file():
                continue
            manifest = read_json(manifest_path)
            if (
                manifest.get("logical_name") != self.logical_name
                or manifest.get("generation_id") != path.name
                or manifest.get("collection_name")
                != self.logical_name + "_g_" + path.name
            ):
                continue
            manifests[path.name] = (resolved, manifest)
        return manifests

    def prune(self, client):
        """Delete owned obsolete generations; retain active, previous, latest."""
        active = self.load_active()
        if active is None:
            raise ValueError("Cannot prune without an active generation")
        keep = {active["generation_id"]}
        if active.get("previous_generation"):
            keep.add(active["previous_generation"])
        latest = self.latest_attempt()
        if latest and latest.get("generation_id"):
            keep.add(latest["generation_id"])

        manifests = self._owned_generation_manifests()
        targets = {
            generation_id: value
            for generation_id, value in manifests.items()
            if generation_id not in keep
        }
        collection_names = {
            item if isinstance(item, str) else item.name
            for item in client.list_collections()
        }
        deleted_collections = []
        deleted_generations = []
        for generation_id, (path, manifest) in sorted(targets.items()):
            collection_name = manifest["collection_name"]
            if collection_name in collection_names:
                client.delete_collection(collection_name)
                deleted_collections.append(collection_name)
            if path.resolve().parent != (self.root / "generations").resolve():
                raise ValueError("Refusing to delete a path outside this logical index")
            shutil.rmtree(path)
            deleted_generations.append(generation_id)
        return {
            "kept_generation_ids": sorted(keep),
            "deleted_generation_ids": deleted_generations,
            "deleted_collection_names": deleted_collections,
        }
