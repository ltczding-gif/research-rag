from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture
def client(monkeypatch):
    service = Path(__file__).resolve().parents[1] / "service"
    monkeypatch.syspath_prepend(str(service))
    saved = {name: sys.modules.pop(name, None) for name in ("config", "embedding_client")}
    try:
        import embedding_client
        yield embedding_client
    finally:
        for name, original in saved.items():
            sys.modules.pop(name, None)
            if original is not None:
                sys.modules[name] = original


def test_ollama_same_dimensions_new_digest_changes_contract(client, monkeypatch):
    monkeypatch.setattr(client, "EMBED_PROVIDER", "ollama")
    monkeypatch.setattr(client, "EMBED_MODEL", "toy:1")
    monkeypatch.setattr(client, "OLLAMA_URL", "http://localhost:11434/api/embeddings")
    digest = ["a" * 64]
    inputs = []

    class Response:
        def __init__(self, value): self.value = value
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps(self.value).encode()

    def request(value, **kwargs):
        url = value if isinstance(value, str) else value.full_url
        if url.endswith("/api/tags"):
            return Response({"models": [{"name": "toy:1", "digest": digest[0]}]})
        if url.endswith("/api/show"):
            return Response({"model_info": {"qwen.context_length": 8192}})
        payload = json.loads(value.data)
        assert payload["truncate"] is False
        inputs.extend(payload["input"])
        return Response({"embeddings": [[1.0, 0.25]]})

    monkeypatch.setattr(client.urllib.request, "urlopen", request)
    first = client.embedding_contract()
    digest[0] = "b" * 64
    second = client.embedding_contract()
    assert first["dimensions"] == second["dimensions"] == 2
    assert first["revision"] != second["revision"]
    assert inputs == ["research-rag model contract probe"] * 2
    with pytest.raises(ValueError, match="truncated"):
        client.embed_index_text("中" * 3000)


def test_fastembed_token_window_preserves_tail_in_separate_unit(client, monkeypatch):
    from tokenizers import Tokenizer, models, pre_tokenizers
    tokenizer = Tokenizer(models.WordLevel({"[UNK]": 0, "one": 1, "two": 2, "three": 3,
                                            "four": 4, "five": 5, "six": 6}, unk_token="[UNK]"))
    tokenizer.pre_tokenizer = pre_tokenizers.Whitespace()
    tokenizer.enable_truncation(3)
    model = SimpleNamespace(model=SimpleNamespace(tokenizer=tokenizer))
    monkeypatch.setattr(client, "EMBED_PROVIDER", "fastembed")
    monkeypatch.setattr(client, "MAX_EMBED_CHARS", 100)
    monkeypatch.setattr(client, "_get_fastembed_model", lambda: model)
    monkeypatch.setattr(client, "get_embedding", lambda text: [1.0, 0.5])
    text = "one two three four five six"
    with pytest.raises(ValueError, match="truncated"):
        client.embed_index_text(text)
    units = client.split_embedding_text(text)
    assert "".join(unit[2] for unit in units) == text
    assert units[-1][2].endswith("six") and len(units) == 2
    assert all(client.embed_index_text(unit[2]) == [1.0, 0.5] for unit in units)
    assert units[0][0] == 0 and units[0][1] == units[1][0] and units[-1][1] == len(text)
    assert client.embed_index_text("one two three   ") == [1.0, 0.5]


def test_tokenizer_offsets_detect_truncation_without_overflow_records(client, monkeypatch):
    tokenizer = SimpleNamespace(encode=lambda text: SimpleNamespace(
        offsets=[(0, 3), (4, 7)], overflowing=[]))
    monkeypatch.setattr(client, "EMBED_PROVIDER", "fastembed")
    monkeypatch.setattr(client, "MAX_EMBED_CHARS", 100)
    monkeypatch.setattr(client, "_get_fastembed_model",
                        lambda: SimpleNamespace(model=SimpleNamespace(tokenizer=tokenizer)))
    with pytest.raises(ValueError, match="truncated"):
        client.embed_index_text("one two omitted")


@pytest.mark.parametrize("vector", [[], [0.0, 0.0], [float("nan"), 1.0], [float("inf")]])
def test_indexing_rejects_invalid_vectors(client, monkeypatch, vector):
    monkeypatch.setattr(client, "EMBED_PROVIDER", "openai-compat")
    monkeypatch.setattr(client, "get_embedding", lambda text: vector)
    with pytest.raises(ValueError, match="invalid vector"):
        client.embed_index_text("complete input")


def test_remote_unversioned_model_cannot_publish_contract(client, monkeypatch):
    monkeypatch.setattr(client, "EMBED_PROVIDER", "openai-compat")
    monkeypatch.delenv("OPENAI_EMBED_REVISION", raising=False)
    monkeypatch.setattr(client, "get_embedding", lambda text: pytest.fail("Must not request a paid probe"))
    with pytest.raises(ValueError, match="OPENAI_EMBED_REVISION"):
        client.embedding_contract()


def test_fastembed_revision_is_bound_to_loaded_artifacts(client, monkeypatch, tmp_path):
    (tmp_path / "model.onnx").write_bytes(b"model version 1")
    (tmp_path / "tokenizer.json").write_text("{}")
    model = [SimpleNamespace(model=SimpleNamespace(_model_dir=tmp_path))]
    monkeypatch.setattr(client, "_get_fastembed_model", lambda: model[0])
    first = client._fastembed_identity()
    (tmp_path / "model.onnx").write_bytes(b"model version 2")
    model[0] = SimpleNamespace(model=SimpleNamespace(_model_dir=tmp_path))
    assert client._fastembed_identity() != first
