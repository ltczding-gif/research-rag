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


class GenerationIntegrityError(ValueError):
    """Persistent state is inconsistent; automatic selection is unsafe."""


class PublicationCommittedError(RuntimeError):
    """The active pointer committed despite a subsequent exception."""

    def __init__(self, generation_id, operation, cause):
        self.generation_id = generation_id
        self.operation = operation
        self.interrupted = isinstance(cause, KeyboardInterrupt)
        super().__init__(
            f"{operation} committed generation {generation_id}; "
            f"subsequent {type(cause).__name__}: {cause}. Inspect index status before retrying."
        )


def _error_text(error):
    return "interrupted" if isinstance(error, KeyboardInterrupt) else str(error) or type(error).__name__


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
        generation_root = self.root / "generations"
        path = generation_root / identifier
        if (generation_root.resolve().parent != self.root.resolve()
                or path.resolve().parent != generation_root.resolve()):
            raise GenerationIntegrityError("Generation path escapes its logical namespace")
        return path

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

    def _validate_manifest(self, manifest, generation_id=None):
        if not isinstance(manifest, dict):
            raise GenerationIntegrityError("Generation manifest must be an object")
        identifier = manifest.get("generation_id")
        if (
            not isinstance(identifier, str)
            or not re.fullmatch(r"[a-f0-9]{32}", identifier)
            or (generation_id is not None and identifier != generation_id)
            or manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("logical_name") != self.logical_name
            or manifest.get("collection_name") != self.logical_name + "_g_" + identifier
            or manifest.get("state") != "complete"
            or not isinstance(manifest.get("item_count"), int)
            or isinstance(manifest.get("item_count"), bool)
            or manifest["item_count"] <= 0
        ):
            raise GenerationIntegrityError("Generation manifest is incomplete or inconsistent")
        return manifest

    def load_active(self):
        pointer_path = self.root / "active.json"
        if not pointer_path.exists():
            if pointer_path.is_symlink():
                raise GenerationIntegrityError("Active pointer is a dangling symlink")
            return None
        try:
            pointer = read_json(pointer_path)
            if not isinstance(pointer, dict):
                raise GenerationIntegrityError("Active pointer must be an object")
            manifest = read_json(self.generation_path(pointer) / "manifest.json")
            self._validate_manifest(manifest, pointer.get("generation_id"))
            if (fingerprint(manifest) != pointer.get("manifest_fingerprint")
                    or manifest["collection_name"] != pointer.get("collection_name")):
                raise GenerationIntegrityError("Active index manifest is incomplete or inconsistent")
        except (OSError, KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, GenerationIntegrityError):
                raise
            raise GenerationIntegrityError(
                "Active index is unreadable or inconsistent; use explicit offline recovery"
            ) from exc
        return manifest

    def publication_state(self, generation):
        """Observe persistent commit state, not a local success flag.

        'not_active' does not imply the generation has never been active.
        'unknown' must never authorize rewriting a sealed manifest.
        """
        try:
            active = self.load_active()
        except (OSError, ValueError, KeyError, TypeError):
            return "unknown"
        if active is None or active["generation_id"] != generation["generation_id"]:
            return "not_active"
        return "committed" if fingerprint(active) == fingerprint(generation) else "unknown"

    def _pointer_for(self, manifest):
        return {
            "generation_id": manifest["generation_id"],
            "collection_name": manifest["collection_name"],
            "manifest_fingerprint": fingerprint(manifest),
        }

    def _activate_manifest(self, manifest, operation):
        try:
            atomic_write_json(self.root / "active.json", self._pointer_for(manifest))
        except BaseException as exc:
            if self.publication_state(manifest) == "committed":
                self._record_error_event(manifest, exc, "committed", operation)
                raise PublicationCommittedError(manifest["generation_id"], operation, exc) from exc
            raise
        return manifest

    def _record_error_event(self, generation, error, publication_state, operation="build"):
        """Best-effort diagnostics cannot invalidate an already sealed version."""
        event = {
            "schema_version": 1,
            "event_id": uuid4().hex,
            "generation_id": generation["generation_id"],
            "logical_name": self.logical_name,
            "operation": operation,
            "recorded_at": now(),
            "publication_state": publication_state,
            "manifest_state": generation.get("state"),
            "error_type": type(error).__name__,
            "error": _error_text(error),
        }
        try:
            atomic_write_json(self.root / "events" / (event["event_id"] + ".json"), event)
            latest = self.latest_attempt()
            # A delayed recorder may run after a newer attempt acquired the lock.
            if latest is None or latest.get("generation_id") == generation["generation_id"]:
                atomic_write_json(self.root / "latest-attempt.json", {
                    "generation_id": generation["generation_id"],
                    "state": generation.get("state"),
                    "started_at": generation.get("started_at"),
                    "finished_at": generation.get("finished_at"),
                    "item_count": generation.get("item_count", 0),
                    "publication_state": publication_state,
                    "error": _error_text(error),
                    "event_id": event["event_id"],
                })
        except Exception:
            return False
        return True

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
        manifest_path = self.generation_path(generation) / "manifest.json"
        if manifest_path.exists():
            previous = read_json(manifest_path)
            if previous.get("state") in {"complete", "failed"}:
                if fingerprint(previous) != fingerprint(generation):
                    raise GenerationIntegrityError("A sealed generation manifest is immutable")
                return
        atomic_write_json(manifest_path, generation)
        atomic_write_json(self.root / "latest-attempt.json", {
            key: generation[key] for key in ("generation_id", "state", "started_at", "item_count")
        } | {"error": generation.get("error"), "finished_at": generation.get("finished_at")})

    def fail(self, generation, error):
        """Record failure while holding writer_lock; never mutate sealed state."""
        try:
            disk = read_json(self.generation_path(generation) / "manifest.json")
            if not isinstance(disk, dict) or disk.get("generation_id") != generation["generation_id"]:
                raise GenerationIntegrityError("Unexpected candidate manifest identity")
        except (OSError, ValueError, KeyError, TypeError):
            self._record_error_event(generation, error, "unknown")
            return "unknown"
        state = self.publication_state(disk)
        if state != "not_active" or disk.get("state") in {"complete", "failed"}:
            self._record_error_event(disk, error, state)
            return state
        disk.update(state="failed", error=_error_text(error), finished_at=now())
        atomic_write_json(self.generation_path(disk) / "manifest.json", disk)
        generation.clear()
        generation.update(disk)
        self._record_error_event(disk, error, state)
        return state

    def generation_usable(self, client, manifest):
        """Complete identity, artifacts and collection are prerequisites to reuse."""
        try:
            self._validate_manifest(manifest)
            self.validate_artifacts(manifest)
            self._validate_collection(client, manifest)
        except Exception:
            return False
        return True

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
        if generation.get("state") != "building":
            raise GenerationIntegrityError("Only a building candidate may be published")
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
        return self._activate_manifest(generation, "publish")

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
        return self._activate_manifest(previous, "rollback")

    def recover(self, client, generation_id, expected_embedding_contract, *,
                expected_manifest_fingerprint):
        """Explicit offline recovery to one independently trusted sealed version.

        Caller holds writer_lock. Preserve the original pointer before replacing
        it. Never choose a target by timestamp or rewrite a target manifest.
        """
        if (not isinstance(expected_manifest_fingerprint, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_manifest_fingerprint)):
            raise ValueError("Recovery requires a trusted manifest fingerprint")
        pointer_path = self.root / "active.json"
        if pointer_path.is_symlink():
            raise GenerationIntegrityError("Refusing recovery through a symlinked active pointer")
        original = pointer_path.read_bytes() if pointer_path.exists() else None
        if original is not None and len(original) > 65536:
            raise GenerationIntegrityError("Active pointer is unexpectedly large")
        try:
            active = self.load_active()
        except GenerationIntegrityError:
            active = None
        else:
            if active is not None:
                raise ValueError("Active manifest is valid; use rollback or rebuild instead")
        target = read_json(self.generation_path(generation_id) / "manifest.json")
        self._validate_manifest(target, generation_id)
        if fingerprint(target) != expected_manifest_fingerprint:
            raise GenerationIntegrityError("Recovery manifest does not match the trusted fingerprint")
        if target.get("contract", {}).get("embedding") != expected_embedding_contract:
            raise ValueError("Recovery embedding contract is incompatible")
        if not target.get("sources") or any(
            not isinstance(source, dict) or source.get("status") != "success"
            for source in target["sources"]
        ):
            raise GenerationIntegrityError("Recovery requires a complete source inventory")
        self.validate_artifacts(target)
        self._validate_collection(client, target)
        atomic_write_json(self.root / "recovery" / (uuid4().hex + ".json"), {
            "schema_version": 1, "operation": "recover-intent", "recorded_at": now(),
            "logical_name": self.logical_name, "generation_id": generation_id,
            "manifest_fingerprint": expected_manifest_fingerprint,
            "previous_pointer_hex": original.hex() if original is not None else None,
            "previous_pointer_sha256": hashlib.sha256(original).hexdigest() if original is not None else None,
        })
        current = pointer_path.read_bytes() if pointer_path.exists() else None
        if current != original:
            raise GenerationIntegrityError("Active pointer changed during recovery")
        return self._activate_manifest(target, "recover")

    def status(self, client=None):
        active_error = latest_error = None
        try:
            active = self.load_active()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            active, active_error = None, str(exc)
        try:
            latest = self.latest_attempt()
        except (OSError, ValueError, KeyError, TypeError) as exc:
            latest, latest_error = None, str(exc)
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
            "active_error": active_error,
            "recovery_required": active_error is not None,
            "latest_attempt_error": latest_error,
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
