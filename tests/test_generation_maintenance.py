from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest


SERVICE_DIR = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE_DIR))

import index_generation  # noqa: E402


def _source(source_id: str) -> dict[str, str]:
    return {
        "source_id": source_id,
        "content_hash": "hash-" + source_id,
        "status": "success",
    }


def _contract(revision: str = "revision-a") -> dict[str, object]:
    return {
        "embedding": {
            "provider": "test",
            "model": "model-a",
            "revision": revision,
            "dimensions": 2,
            "distance": "cosine",
        },
        "pipeline": {"schema": 1},
    }


class _Collection:
    def __init__(self, generation_id: str, count: int):
        self.metadata = {"generation_id": generation_id}
        self._count = count

    def count(self):
        return self._count


class _Named:
    def __init__(self, name: str):
        self.name = name


class _Client:
    def __init__(self):
        self.collections: dict[str, _Collection] = {}
        self.deleted: list[str] = []

    def get_collection(self, name: str):
        if name not in self.collections:
            raise KeyError(name)
        return self.collections[name]

    def list_collections(self):
        return [_Named(name) for name in self.collections]

    def delete_collection(self, name: str):
        self.deleted.append(name)
        del self.collections[name]


def _publish(store, client, *, source_id: str, revision: str = "revision-a"):
    candidate = store.begin(_contract(revision), [_source(source_id)])
    (store.generation_path(candidate) / "pages.jsonl").write_text(
        '{"page":1}\n', encoding="utf-8"
    )
    store.publish(
        candidate,
        item_count=1,
        artifacts={"pages": "pages.jsonl"},
        allow_removals=True,
    )
    client.collections[candidate["collection_name"]] = _Collection(
        candidate["generation_id"], 1
    )
    return candidate


def test_rollback_validates_previous_and_does_not_rewrite_manifests(tmp_path):
    store = index_generation.GenerationStore(tmp_path, "papers")
    client = _Client()
    previous = _publish(store, client, source_id="source-a")
    current = _publish(store, client, source_id="source-b")
    previous_path = store.generation_path(previous) / "manifest.json"
    current_path = store.generation_path(current) / "manifest.json"
    before = (previous_path.read_bytes(), current_path.read_bytes())

    rolled_back = store.rollback(client, _contract()["embedding"])

    assert rolled_back["generation_id"] == previous["generation_id"]
    assert store.load_active()["generation_id"] == previous["generation_id"]
    assert (previous_path.read_bytes(), current_path.read_bytes()) == before
    assert rolled_back["artifact_hashes"]["pages.jsonl"]


@pytest.mark.parametrize("failure", ["count", "identity", "embedding", "manifest"])
def test_rollback_failure_preserves_active_pointer(tmp_path, failure):
    store = index_generation.GenerationStore(tmp_path, "papers")
    client = _Client()
    previous = _publish(store, client, source_id="source-a")
    current = _publish(store, client, source_id="source-b")
    pointer_path = store.root / "active.json"
    pointer_before = pointer_path.read_bytes()
    expected_embedding = _contract()["embedding"]

    if failure == "count":
        client.collections[previous["collection_name"]]._count = 2
    elif failure == "identity":
        client.collections[previous["collection_name"]].metadata["generation_id"] = "bad"
    elif failure == "embedding":
        expected_embedding = _contract("other-revision")["embedding"]
    else:
        manifest_path = store.generation_path(previous) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["item_count"] = 2
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError):
        store.rollback(client, expected_embedding)

    assert pointer_path.read_bytes() == pointer_before
    assert json.loads(pointer_before)["generation_id"] == current["generation_id"]


def test_rollback_pointer_replace_failure_keeps_current_active(
    tmp_path, monkeypatch
):
    store = index_generation.GenerationStore(tmp_path, "papers")
    client = _Client()
    _publish(store, client, source_id="source-a")
    current = _publish(store, client, source_id="source-b")
    pointer_path = store.root / "active.json"
    pointer_before = pointer_path.read_bytes()
    real_replace = index_generation.os.replace

    def fail_pointer(source, destination):
        if Path(destination) == pointer_path:
            raise OSError("injected rollback pointer failure")
        return real_replace(source, destination)

    monkeypatch.setattr(index_generation.os, "replace", fail_pointer)
    with pytest.raises(OSError, match="injected"):
        store.rollback(client, _contract()["embedding"])

    assert pointer_path.read_bytes() == pointer_before
    assert json.loads(pointer_before)["generation_id"] == current["generation_id"]


def test_prune_keeps_active_previous_and_latest_and_ignores_other_namespaces(
    tmp_path,
):
    store = index_generation.GenerationStore(tmp_path, "papers")
    other = index_generation.GenerationStore(tmp_path, "notes")
    client = _Client()
    first = _publish(store, client, source_id="source-1")
    second = _publish(store, client, source_id="source-2")
    previous = _publish(store, client, source_id="source-3")
    active = _publish(store, client, source_id="source-4")
    latest = store.begin(_contract(), [_source("failed-source")])
    store.fail(latest, "injected")
    other_generation = _publish(other, client, source_id="note-1")
    client.collections["legacy-papers"] = _Collection("legacy", 99)
    invalid_dir = store.root / "generations" / "not-a-generation"
    invalid_dir.mkdir()
    (invalid_dir / "manifest.json").write_text("{}", encoding="utf-8")

    result = store.prune(client)

    assert set(result["deleted_generation_ids"]) == {
        first["generation_id"],
        second["generation_id"],
    }
    assert store.generation_path(previous).is_dir()
    assert store.generation_path(active).is_dir()
    assert store.generation_path(latest).is_dir()
    assert other.generation_path(other_generation).is_dir()
    assert invalid_dir.is_dir()
    assert "legacy-papers" in client.collections
    assert other_generation["collection_name"] in client.collections
    assert set(client.deleted) == {
        first["collection_name"],
        second["collection_name"],
    }


def test_prune_requires_active_and_status_reports_collection_mismatch(tmp_path):
    empty = index_generation.GenerationStore(tmp_path, "empty")
    with pytest.raises(ValueError, match="without an active"):
        empty.prune(_Client())

    store = index_generation.GenerationStore(tmp_path, "papers")
    client = _Client()
    active = _publish(store, client, source_id="source-a")
    client.collections[active["collection_name"]]._count = 3

    status = store.status(client)

    assert status["active_collection_valid"] is False
    assert "count" in status["active_collection_error"]
    assert Path(status["root"]).is_absolute()

    (store.generation_path(active) / "pages.jsonl").write_text(
        "changed\n", encoding="utf-8"
    )
    corrupted = store.status(client)
    assert corrupted["active_artifacts_valid"] is False
    assert "hash mismatch" in corrupted["active_artifacts_error"]


def test_publish_rejects_artifact_path_outside_generation(tmp_path):
    store = index_generation.GenerationStore(tmp_path, "papers")
    candidate = store.begin(_contract(), [_source("source-a")])
    outside = store.generation_path(candidate).parent / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    with pytest.raises(ValueError, match="escapes"):
        store.publish(
            candidate,
            item_count=1,
            artifacts={"pages": "../outside.txt"},
        )

    assert store.load_active() is None


def test_offline_status_cli_uses_only_explicit_temp_namespace(tmp_path):
    chroma_path = tmp_path / "chroma"
    chroma_path.mkdir()
    script = Path(__file__).resolve().parents[1] / "scripts" / "manage_index_generations.py"

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--chroma-path",
            str(chroma_path),
            "--logical-name",
            "papers-test",
            "status",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr + completed.stdout
    result = json.loads(completed.stdout)
    assert result["logical_name"] == "papers-test"
    assert result["active"] is None
    assert str(chroma_path.resolve()) in result["root"]
