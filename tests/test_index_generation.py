"""Persistence and publication contracts for immutable index generations."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture
def generation(monkeypatch):
    service_dir = Path(__file__).resolve().parents[1] / "service"
    monkeypatch.syspath_prepend(str(service_dir))
    saved = sys.modules.pop("index_generation", None)
    try:
        import index_generation

        yield index_generation
    finally:
        sys.modules.pop("index_generation", None)
        if saved is not None:
            sys.modules["index_generation"] = saved


def _source(source_id: str, content_hash: str, *, status: str = "success") -> dict:
    return {
        "source_id": source_id,
        "content_hash": content_hash,
        "status": status,
    }


def _contract(model: str = "model-a", revision: str = "revision-a") -> dict:
    return {
        "embedding": {
            "provider": "test",
            "model": model,
            "revision": revision,
            "dimensions": 2,
            "distance": "cosine",
        },
        "pipeline": {"schema": 1},
    }


def _publish(store, *, contract=None, sources=None, item_count=1, allow_removals=False):
    contract = contract or _contract()
    sources = sources or [_source("source-a", "hash-a")]
    candidate = store.begin(contract, sources)
    store.publish(
        candidate,
        item_count=item_count,
        artifacts={"record_count": item_count},
        allow_removals=allow_removals,
    )
    return candidate


def test_building_and_failed_candidates_do_not_replace_active(generation, tmp_path):
    store = generation.GenerationStore(tmp_path, "papers")
    active = _publish(store)
    active_pointer = (store.root / "active.json").read_bytes()

    building = store.begin(
        _contract(model="model-b", revision="revision-b"),
        [_source("source-a", "hash-a")],
    )
    assert store.load_active()["generation_id"] == active["generation_id"]
    assert (store.root / "active.json").read_bytes() == active_pointer

    store.fail(building, "injected candidate failure")
    assert store.load_active()["generation_id"] == active["generation_id"]
    assert (store.root / "active.json").read_bytes() == active_pointer
    assert store.latest_attempt()["generation_id"] == building["generation_id"]
    assert store.latest_attempt()["state"] == "failed"


def test_same_dimensions_with_different_model_identity_is_not_reused(
    generation, tmp_path
):
    store = generation.GenerationStore(tmp_path, "notes")
    sources = [_source("note-a", "hash-a")]
    first_contract = _contract(model="model-a", revision="digest-a")
    first = _publish(store, contract=first_contract, sources=sources)
    active = store.load_active()
    second_contract = _contract(model="model-b", revision="digest-b")

    assert first_contract["embedding"]["dimensions"] == 2
    assert second_contract["embedding"]["dimensions"] == 2
    assert store.same_inputs(active, second_contract, sources) is False

    second = _publish(store, contract=second_contract, sources=sources)
    assert second["generation_id"] != first["generation_id"]
    assert store.load_active()["contract"]["embedding"]["revision"] == "digest-b"


def test_same_content_rename_does_not_require_removal_authorization(
    generation, tmp_path
):
    store = generation.GenerationStore(tmp_path, "papers")
    _publish(
        store,
        sources=[
            _source("old-name.pdf", "content-a"),
            _source("stable.pdf", "content-b"),
        ],
        item_count=2,
    )

    renamed = _publish(
        store,
        sources=[
            _source("new-name.pdf", "content-a"),
            _source("stable.pdf", "content-b"),
        ],
        item_count=2,
    )

    assert [source["source_id"] for source in store.load_active()["sources"]] == [
        "new-name.pdf",
        "stable.pdf",
    ]
    assert renamed["previous_generation"] is not None


def test_real_source_withdrawal_requires_explicit_authorization(
    generation, tmp_path
):
    store = generation.GenerationStore(tmp_path, "papers")
    previous = _publish(
        store,
        sources=[
            _source("old-name.pdf", "content-a"),
            _source("removed.pdf", "content-b"),
        ],
        item_count=2,
    )
    candidate = store.begin(
        _contract(), [_source("new-name.pdf", "content-a")]
    )

    with pytest.raises(ValueError, match="allow-removals"):
        store.publish(candidate, item_count=1, artifacts={})
    assert store.load_active()["generation_id"] == previous["generation_id"]

    store.publish(candidate, item_count=1, artifacts={}, allow_removals=True)
    assert [source["source_id"] for source in store.load_active()["sources"]] == [
        "new-name.pdf"
    ]


def test_duplicate_content_withdrawal_still_requires_authorization(
    generation, tmp_path
):
    """One rename cannot silently cover removal of a second identical source."""
    store = generation.GenerationStore(tmp_path, "papers")
    previous = _publish(
        store,
        sources=[
            _source("copy-a.pdf", "shared-content"),
            _source("copy-b.pdf", "shared-content"),
        ],
        item_count=2,
    )
    candidate = store.begin(
        _contract(), [_source("renamed-copy.pdf", "shared-content")]
    )

    with pytest.raises(ValueError, match="allow-removals"):
        store.publish(candidate, item_count=1, artifacts={})
    assert store.load_active()["generation_id"] == previous["generation_id"]


def test_publish_requires_complete_successful_source_list(generation, tmp_path):
    store = generation.GenerationStore(tmp_path, "notes")
    incomplete = [
        _source("note-a", "hash-a"),
        _source("note-b", "hash-b", status="failed"),
    ]
    failed = store.begin(_contract(), incomplete)

    with pytest.raises(ValueError, match="Every declared source must succeed"):
        store.publish(failed, item_count=2, artifacts={})
    assert store.load_active() is None

    complete = [
        _source("note-a", "hash-a"),
        _source("note-b", "hash-b"),
    ]
    published = _publish(store, sources=complete, item_count=2)
    active = store.load_active()
    assert active["generation_id"] == published["generation_id"]
    assert active["sources"] == complete
    assert active["item_count"] == 2


def test_writer_lock_blocks_second_owner_and_releases_after_exception(
    generation, tmp_path
):
    first = generation.GenerationStore(tmp_path, "papers")
    second = generation.GenerationStore(tmp_path, "papers")

    with pytest.raises(RuntimeError, match="injected"):
        with first.writer_lock():
            with pytest.raises(RuntimeError, match="Another index builder"):
                with second.writer_lock():
                    pytest.fail("second writer unexpectedly acquired the lock")
            raise RuntimeError("injected owner failure")

    with second.writer_lock():
        assert (second.root / "writer.lock").exists()


def test_writer_lock_is_released_when_owner_process_exits(generation, tmp_path):
    service_dir = Path(generation.__file__).resolve().parent
    ready = tmp_path / "owner-entered.txt"
    script = "\n".join(
        [
            "import os, sys",
            "from pathlib import Path",
            f"sys.path.insert(0, {str(service_dir)!r})",
            "from index_generation import GenerationStore",
            f"store = GenerationStore({str(tmp_path)!r}, 'crash-lock')",
            "with store.writer_lock():",
            f"    Path({str(ready)!r}).write_text('locked', encoding='utf-8')",
            "    os._exit(0)",
        ]
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    assert ready.read_text(encoding="utf-8") == "locked"
    store = generation.GenerationStore(tmp_path, "crash-lock")
    with store.writer_lock():
        pass


def test_active_pointer_replace_failure_preserves_previous_active(
    generation, tmp_path, monkeypatch
):
    store = generation.GenerationStore(tmp_path, "papers")
    previous = _publish(store)
    pointer_path = store.root / "active.json"
    original_pointer = pointer_path.read_bytes()
    candidate = store.begin(
        _contract(model="model-b", revision="digest-b"),
        [_source("source-a", "hash-a")],
    )
    real_replace = generation.os.replace

    def fail_active_replace(source, destination):
        if Path(destination) == pointer_path:
            raise OSError("injected active pointer failure")
        return real_replace(source, destination)

    monkeypatch.setattr(generation.os, "replace", fail_active_replace)
    with pytest.raises(OSError, match="injected active pointer failure"):
        store.publish(candidate, item_count=1, artifacts={})

    assert pointer_path.read_bytes() == original_pointer
    assert store.load_active()["generation_id"] == previous["generation_id"]
    assert not tuple(pointer_path.parent.glob(f".{pointer_path.name}*.tmp"))


def test_load_active_rejects_manifest_tampering(generation, tmp_path):
    store = generation.GenerationStore(tmp_path, "papers")
    published = _publish(store)
    manifest_path = store.generation_path(published) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["item_count"] = 999
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="incomplete or inconsistent"):
        store.load_active()


def test_implementation_contract_binds_file_bytes_and_semantic_settings(
    generation, tmp_path
):
    implementation = tmp_path / "builder.py"
    implementation.write_text("VERSION = 1\n", encoding="utf-8")
    first = generation.implementation_contract(
        [implementation], {"chunk_size": 800}
    )
    implementation.write_text("VERSION = 2\n", encoding="utf-8")
    changed_code = generation.implementation_contract(
        [implementation], {"chunk_size": 800}
    )
    changed_setting = generation.implementation_contract(
        [implementation], {"chunk_size": 1200}
    )

    assert first != changed_code
    assert changed_code != changed_setting
