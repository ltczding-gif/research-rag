"""Concurrency and crash-recovery contracts for the HTTP query-log writer."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import importlib
import json
import os
from pathlib import Path
import sys
from threading import Barrier

import pytest


@pytest.fixture(scope="module")
def query_server():
    pytest.importorskip("chromadb")
    pytest.importorskip("flask")
    repo_root = Path(__file__).resolve().parent.parent
    service_dir = repo_root / "service"
    module_names = ("config", "embedding_client", "index_generation", "query_server")
    saved_modules = {name: sys.modules.pop(name, None) for name in module_names}
    saved_skip = os.environ.get("LOCALRAG_SKIP_CHROMA_INIT")
    sys.path.insert(0, str(service_dir))
    os.environ["LOCALRAG_SKIP_CHROMA_INIT"] = "1"
    try:
        module = importlib.import_module("query_server")
        yield module
    finally:
        try:
            sys.path.remove(str(service_dir))
        except ValueError:
            pass
        for name in module_names:
            sys.modules.pop(name, None)
            if saved_modules[name] is not None:
                sys.modules[name] = saved_modules[name]
        if saved_skip is None:
            os.environ.pop("LOCALRAG_SKIP_CHROMA_INIT", None)
        else:
            os.environ["LOCALRAG_SKIP_CHROMA_INIT"] = saved_skip


def _payload(idempotency_key: str, *, short_id: str | None = None) -> dict:
    payload = {
        "workflow_id": "WF1a",
        "workflow_name": "Concurrency test",
        "status": "success",
        "query": "temporary query-log test",
        "anchor_query": "temporary query-log test",
        "final_response_snapshot": "temporary response",
        "idempotency_key": idempotency_key,
        "planned_angles": ["anchor"],
        "executed_angles": ["anchor"],
        "search_runs": [{"query": "temporary query-log test", "hits": 1}],
        "created_at": "2026-09-17T12:00:00+08:00",
    }
    if short_id is not None:
        payload["short_id"] = short_id
    return payload


def _concurrent_posts(app, endpoint: str, payloads: list[dict]):
    barrier = Barrier(len(payloads), timeout=10)

    def post(payload):
        barrier.wait()
        with app.test_client() as client:
            response = client.post(endpoint, json=payload)
            return response.status_code, response.get_json()

    with ThreadPoolExecutor(max_workers=len(payloads)) as executor:
        return list(executor.map(post, payloads))


def test_distinct_concurrent_creates_preserve_both_registry_entries(
    query_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(query_server, "QUERY_LOG_ROOT", tmp_path)

    responses = _concurrent_posts(
        query_server.app,
        "/write_query_log",
        [
            _payload("session-a", short_id="A001"),
            _payload("session-b", short_id="B001"),
        ],
    )

    assert [status for status, _ in responses] == [200, 200]
    assert all(body["created"] is True for _, body in responses)
    assert set(query_server.load_query_log_registry()) == {"session-a", "session-b"}
    assert len(tuple(tmp_path.rglob("*.md"))) == 2


def test_same_key_concurrent_creates_produce_one_log_and_one_deduplication(
    query_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(query_server, "QUERY_LOG_ROOT", tmp_path)

    responses = _concurrent_posts(
        query_server.app,
        "/write_query_log",
        [_payload("same-session"), _payload("same-session")],
    )

    assert [status for status, _ in responses] == [200, 200]
    bodies = [body for _, body in responses]
    assert sorted(body["created"] for body in bodies) == [False, True]
    assert sorted(body["deduplicated"] for body in bodies) == [False, True]
    assert len({body["log_path"] for body in bodies}) == 1
    assert len({body["log_id"] for body in bodies}) == 1
    assert len(tuple(tmp_path.rglob("*.md"))) == 1
    assert set(query_server.load_query_log_registry()) == {"same-session"}


def test_distinct_keys_cannot_overwrite_the_same_derived_log_path(
    query_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(query_server, "QUERY_LOG_ROOT", tmp_path)
    first_payload = _payload("first-session", short_id="SAME")
    second_payload = _payload("second-session", short_id="SAME")

    with query_server.app.test_client() as client:
        first = client.post("/write_query_log", json=first_payload)
        second = client.post("/write_query_log", json=second_payload)

    assert first.status_code == 200
    assert second.status_code == 409
    first_body = first.get_json()
    log_path = Path(first_body["log_path"])
    frontmatter, _ = query_server.parse_frontmatter(
        log_path.read_text(encoding="utf-8")
    )
    assert frontmatter["idempotency_key"] == "first-session"
    assert len(tuple(tmp_path.rglob("*.md"))) == 1
    assert set(query_server.load_query_log_registry()) == {"first-session"}


def test_concurrent_appends_preserve_both_actions_once(
    query_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(query_server, "QUERY_LOG_ROOT", tmp_path)
    with query_server.app.test_client() as client:
        created = client.post(
            "/write_query_log", json=_payload("append-session", short_id="APP1")
        )
    assert created.status_code == 200
    created_body = created.get_json()

    base = {
        "log_path": created_body["log_path"],
        "log_id": created_body["log_id"],
    }
    responses = _concurrent_posts(
        query_server.app,
        "/append_query_log_action",
        [
            {**base, "action": "action-a", "result": "result-a"},
            {**base, "action": "action-b", "result": "result-b"},
        ],
    )

    assert [status for status, _ in responses] == [200, 200]
    content = Path(created_body["log_path"]).read_text(encoding="utf-8")
    assert content.count("- Action: action-a") == 1
    assert content.count("- Action: action-b") == 1
    assert content.count("- Result: result-a") == 1
    assert content.count("- Result: result-b") == 1


def test_retry_recovers_orphan_after_log_commit_before_registry_commit(
    query_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(query_server, "QUERY_LOG_ROOT", tmp_path)
    real_save = query_server.save_query_log_registry
    monkeypatch.setattr(
        query_server,
        "save_query_log_registry",
        lambda registry, root=None: (_ for _ in ()).throw(
            OSError("injected registry failure")
        ),
    )
    payload = _payload("orphan-session", short_id="ORPH")

    with query_server.app.test_client() as client:
        failed = client.post("/write_query_log", json=payload)

    assert failed.status_code == 500
    orphaned_logs = tuple(tmp_path.rglob("*.md"))
    assert len(orphaned_logs) == 1
    assert not Path(query_server.get_query_log_registry_path()).exists()

    monkeypatch.setattr(query_server, "save_query_log_registry", real_save)
    with query_server.app.test_client() as client:
        recovered = client.post("/write_query_log", json=payload)

    assert recovered.status_code == 200
    recovered_body = recovered.get_json()
    assert recovered_body["created"] is False
    assert recovered_body["deduplicated"] is True
    assert recovered_body["recovered"] is True
    assert Path(recovered_body["log_path"]) == orphaned_logs[0].resolve()
    assert len(tuple(tmp_path.rglob("*.md"))) == 1
    registry = query_server.load_query_log_registry()
    assert registry["orphan-session"]["log_path"] == str(orphaned_logs[0].resolve())


@pytest.mark.parametrize(
    "registry_text",
    ("{broken", json.dumps({"broken-entry": {}})),
    ids=("invalid-json", "invalid-entry-shape"),
)
def test_corrupt_registry_fails_closed_without_creating_a_log(
    query_server, tmp_path, monkeypatch, registry_text
):
    monkeypatch.setattr(query_server, "QUERY_LOG_ROOT", tmp_path)
    registry_path = Path(query_server.get_query_log_registry_path())
    registry_path.write_text(registry_text, encoding="utf-8")

    with query_server.app.test_client() as client:
        response = client.post(
            "/write_query_log", json=_payload("blocked-session", short_id="FAIL")
        )

    assert response.status_code == 500
    assert registry_path.read_text(encoding="utf-8") == registry_text
    assert tuple(tmp_path.rglob("*.md")) == ()


def test_append_replace_failure_preserves_original_file(
    query_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(query_server, "QUERY_LOG_ROOT", tmp_path)
    with query_server.app.test_client() as client:
        created = client.post(
            "/write_query_log", json=_payload("replace-session", short_id="RPLC")
        )
    created_body = created.get_json()
    log_path = Path(created_body["log_path"])
    original = log_path.read_bytes()
    generation = sys.modules["index_generation"]
    real_replace = generation.os.replace

    def fail_log_replace(source, destination):
        if Path(destination) == log_path:
            raise OSError("injected log replace failure")
        return real_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(generation.os, "replace", fail_log_replace)
        with query_server.app.test_client() as client:
            response = client.post(
                "/append_query_log_action",
                json={
                    "log_path": str(log_path),
                    "log_id": created_body["log_id"],
                    "action": "must-not-appear",
                    "result": "injected failure",
                },
            )

    assert response.status_code == 500
    assert log_path.read_bytes() == original
    assert not tuple(log_path.parent.glob(f".{log_path.name}*.tmp"))


def test_registry_replace_failure_preserves_previous_json(
    query_server, tmp_path, monkeypatch
):
    monkeypatch.setattr(query_server, "QUERY_LOG_ROOT", tmp_path)
    initial = {
        "existing": {
            "log_id": "ql-existing",
            "log_path": str(tmp_path / "existing.md"),
            "month": "2026-09",
            "created_at": "2026-09-17T12:00:00+08:00",
        }
    }
    query_server.save_query_log_registry(initial)
    registry_path = Path(query_server.get_query_log_registry_path())
    original = registry_path.read_bytes()
    generation = sys.modules["index_generation"]
    real_replace = generation.os.replace

    def fail_registry_replace(source, destination):
        if Path(destination) == registry_path:
            raise OSError("injected registry replace failure")
        return real_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(generation.os, "replace", fail_registry_replace)
        with pytest.raises(OSError, match="injected registry replace failure"):
            query_server.save_query_log_registry({**initial, "new": {}})

    assert registry_path.read_bytes() == original
    assert query_server.load_query_log_registry() == initial
    assert not tuple(tmp_path.glob(f".{registry_path.name}*.tmp"))
