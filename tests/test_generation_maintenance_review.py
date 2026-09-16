from __future__ import annotations

from pathlib import Path
import sys

import pytest


SERVICE_DIR = Path(__file__).resolve().parents[1] / "service"
sys.path.insert(0, str(SERVICE_DIR))

from index_generation import GenerationStore  # noqa: E402


class _Collection:
    def __init__(self, generation_id: str):
        self.metadata = {"generation_id": generation_id}

    def count(self):
        return 1


class _Client:
    def __init__(self):
        self.collections = {}

    def get_collection(self, name: str):
        return self.collections[name]


def _publish(store: GenerationStore, client: _Client, source_id: str):
    contract = {
        "embedding": {
            "provider": "test",
            "model": "model-a",
            "revision": "r1",
            "dimensions": 2,
        }
    }
    candidate = store.begin(
        contract,
        [
            {
                "source_id": source_id,
                "content_hash": "hash-" + source_id,
                "status": "success",
            }
        ],
    )
    artifact = store.generation_path(candidate) / "pages.jsonl"
    artifact.write_text('{"page": 1}\n', encoding="utf-8")
    store.publish(
        candidate,
        item_count=1,
        artifacts={"pages": "pages.jsonl"},
        allow_removals=True,
    )
    client.collections[candidate["collection_name"]] = _Collection(
        candidate["generation_id"]
    )
    return candidate


def test_rollback_rejects_previous_generation_with_missing_artifact(tmp_path):
    store = GenerationStore(tmp_path, "papers")
    client = _Client()
    previous = _publish(store, client, "source-a")
    current = _publish(store, client, "source-b")
    pointer_path = store.root / "active.json"
    pointer_before = pointer_path.read_bytes()
    (store.generation_path(previous) / "pages.jsonl").unlink()

    with pytest.raises(ValueError, match="artifact"):
        store.rollback(client, previous["contract"]["embedding"])

    assert pointer_path.read_bytes() == pointer_before
    assert store.load_active()["generation_id"] == current["generation_id"]
