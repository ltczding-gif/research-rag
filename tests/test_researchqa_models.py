from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks import researchqa_models as models


def _vector(value: float = 0.25, *, dimensions: int = 2560) -> list[float]:
    return [value] * dimensions


class FakeTransport:
    def __init__(
        self,
        *,
        digest: str = models.OLLAMA_EMBED_MODEL_DIGEST,
        response_model: str = models.OLLAMA_EMBED_MODEL_ID,
        dimensions: int = models.OLLAMA_EMBED_DIMENSIONS,
        release_response=None,
    ):
        self.digest = digest
        self.response_model = response_model
        self.dimensions = dimensions
        self.release_response = release_response
        self.calls = []

    def request_json(
        self,
        method,
        url,
        *,
        payload,
        timeout_seconds,
    ):
        self.calls.append((method, url, payload, timeout_seconds))
        if url.endswith("/api/tags"):
            return {
                "models": [
                    {
                        "name": models.OLLAMA_EMBED_MODEL_ID,
                        "model": models.OLLAMA_EMBED_MODEL_ID,
                        "digest": self.digest,
                        "details": {
                            "family": "qwen3",
                            "parameter_size": "4B",
                        },
                    }
                ]
            }
        if url.endswith("/api/generate"):
            if self.release_response is not None:
                return self.release_response
            return {
                "model": models.OLLAMA_EMBED_MODEL_ID,
                "created_at": "2026-07-28T00:00:00Z",
                "response": "",
                "done": True,
                "done_reason": "unload",
            }
        assert url.endswith("/api/embed")
        assert payload["truncate"] is False
        inputs = payload["input"]
        return {
            "model": self.response_model,
            "embeddings": [
                _vector(float(index + 1), dimensions=self.dimensions)
                for index, _text in enumerate(inputs)
            ],
        }


def test_ollama_preflight_proves_tags_digest_and_batch_dimension(tmp_path):
    transport = FakeTransport()
    client = models.OllamaBatchEmbeddingClient(
        cache_dir=tmp_path,
        timeout_seconds=12.5,
        transport=transport,
    )

    preflight = client.preflight()

    assert preflight.provider == "ollama"
    assert preflight.model_id == models.OLLAMA_EMBED_MODEL_ID
    assert preflight.revision == models.OLLAMA_EMBED_MODEL_DIGEST
    assert preflight.dimensions == 2560
    assert len(preflight.fingerprint) == 64
    assert transport.calls == [
        (
            "GET",
            "http://localhost:11434/api/tags",
            None,
            12.5,
        ),
        (
            "POST",
            "http://localhost:11434/api/embed",
            {
                "model": models.OLLAMA_EMBED_MODEL_ID,
                "input": ["research-rag rq2 dimension probe"],
                "truncate": False,
            },
            12.5,
        ),
    ]


def test_ollama_preflight_fails_before_embedding_on_digest_mismatch(tmp_path):
    transport = FakeTransport(digest="0" * 64)
    client = models.OllamaBatchEmbeddingClient(
        cache_dir=tmp_path,
        transport=transport,
    )

    with pytest.raises(models.ModelPreflightError, match="digest mismatch"):
        client.preflight()

    assert len(transport.calls) == 1
    assert transport.calls[0][1].endswith("/api/tags")


@pytest.mark.parametrize(
    ("response_model", "dimensions", "message"),
    [
        ("wrong-model", 2560, "returned model"),
        (models.OLLAMA_EMBED_MODEL_ID, 3, "expected 2560"),
    ],
)
def test_ollama_preflight_rejects_wrong_model_or_dimensions(
    tmp_path,
    response_model,
    dimensions,
    message,
):
    client = models.OllamaBatchEmbeddingClient(
        cache_dir=tmp_path,
        transport=FakeTransport(
            response_model=response_model,
            dimensions=dimensions,
        ),
    )

    with pytest.raises(models.ModelResponseError, match=message):
        client.preflight()


def test_ollama_batches_only_cache_misses_and_preserves_duplicate_order(tmp_path):
    transport = FakeTransport()
    client = models.OllamaBatchEmbeddingClient(
        cache_dir=tmp_path,
        normalization_revision="nfkc-v7",
        transport=transport,
    )

    first = client.embed_texts(("alpha", "beta", "alpha"))
    calls_after_first = len(transport.calls)
    second = client.embed_texts(("alpha", "beta", "alpha"))

    assert calls_after_first == 3  # tags, one-vector probe, one two-vector batch
    batch_call = transport.calls[-1]
    assert batch_call[2] == {
        "model": models.OLLAMA_EMBED_MODEL_ID,
        "input": ["alpha", "beta"],
        "truncate": False,
    }
    assert first[0] == first[2]
    assert first == second
    assert len(transport.calls) == calls_after_first
    assert client.last_cache_hits == 2
    assert client.last_cache_misses == 0
    assert len(tuple(tmp_path.rglob("*.json"))) == 2
    assert tuple(tmp_path.rglob("*.tmp")) == ()


def test_ollama_corrupt_cache_is_rejected_not_silently_reembedded(tmp_path):
    transport = FakeTransport()
    first_client = models.OllamaBatchEmbeddingClient(
        cache_dir=tmp_path,
        transport=transport,
    )
    first_client.embed_texts(("alpha",))
    cache_key = first_client._cache_key("alpha")
    first_client._cache_path(cache_key).write_text(
        '{"cache_key":"wrong"}',
        encoding="utf-8",
    )
    second_client = models.OllamaBatchEmbeddingClient(
        cache_dir=tmp_path,
        transport=transport,
    )

    with pytest.raises(models.ModelCacheError, match="metadata mismatch"):
        second_client.embed_texts(("alpha",))

    # The second client did tags + dimension probe, but no replacement embed.
    assert len(transport.calls) == 5


def test_ollama_transport_failure_is_not_retried(tmp_path):
    class FailingTransport:
        def __init__(self):
            self.calls = 0

        def request_json(self, *args, **kwargs):
            self.calls += 1
            raise TimeoutError("timed out")

    transport = FailingTransport()
    client = models.OllamaBatchEmbeddingClient(
        cache_dir=tmp_path,
        transport=transport,
    )

    with pytest.raises(models.ModelTransportError, match="timed out"):
        client.preflight()

    assert transport.calls == 1


def test_ollama_base_url_cannot_override_fixed_api_paths(tmp_path):
    with pytest.raises(ValueError, match="must not contain an API path"):
        models.OllamaBatchEmbeddingClient(
            cache_dir=tmp_path,
            base_url="http://localhost:11434/api/embeddings",
        )


def test_ollama_release_uses_fixed_keep_alive_zero_request_and_is_idempotent(
    tmp_path,
):
    transport = FakeTransport()
    client = models.OllamaBatchEmbeddingClient(
        cache_dir=tmp_path,
        timeout_seconds=7.0,
        transport=transport,
    )
    client.preflight()

    assert client.release_model() is True
    assert client.release_model() is False
    assert transport.calls[-1] == (
        "POST",
        "http://localhost:11434/api/generate",
        {
            "model": models.OLLAMA_EMBED_MODEL_ID,
            "keep_alive": 0,
        },
        7.0,
    )
    assert sum(
        url.endswith("/api/generate")
        for _method, url, _payload, _timeout in transport.calls
    ) == 1


@pytest.mark.parametrize(
    "release_response",
    [
        {
            "model": "wrong-model",
            "response": "",
            "done": True,
            "done_reason": "unload",
        },
        {
            "model": models.OLLAMA_EMBED_MODEL_ID,
            "created_at": "2026-07-28T00:00:00Z",
            "response": "",
            "done": True,
        },
        {
            "model": models.OLLAMA_EMBED_MODEL_ID,
            "created_at": "2026-07-28T00:00:00Z",
            "response": "not empty",
            "done": True,
            "done_reason": "unload",
        },
        {
            "model": models.OLLAMA_EMBED_MODEL_ID,
            "created_at": "2026-07-28T00:00:00Z",
            "response": "",
            "done": 1,
            "done_reason": "unload",
        },
    ],
)
def test_ollama_release_strictly_rejects_mismatched_response(
    tmp_path,
    release_response,
):
    transport = FakeTransport(release_response=release_response)
    client = models.OllamaBatchEmbeddingClient(
        cache_dir=tmp_path,
        transport=transport,
    )
    client.preflight()

    with pytest.raises(models.ModelResponseError, match="unload response"):
        client.release_model()

    assert transport.calls[-1][1].endswith("/api/generate")


def test_ollama_cache_only_requires_preflight_and_never_uses_network(tmp_path):
    transport = FakeTransport()
    client = models.OllamaBatchEmbeddingClient(
        cache_dir=tmp_path,
        transport=transport,
    )

    with pytest.raises(models.ModelPreflightError, match="requires"):
        client.enter_cache_only()

    expected = client.embed_texts(("alpha",))
    assert client.release_model() is True
    client.enter_cache_only()
    calls_before_cache_only_reads = len(transport.calls)

    assert client.embed_texts(("alpha", "alpha")) == (
        expected[0],
        expected[0],
    )
    with pytest.raises(models.ModelCacheError, match="cache-only.*miss"):
        client.embed_texts(("not-cached",))

    assert len(transport.calls) == calls_before_cache_only_reads
    assert client.last_cache_hits == 0
    assert client.last_cache_misses == 1


class FakeScoreVector:
    def __init__(self, values):
        self.values = list(values)

    def __sub__(self, other):
        return FakeScoreVector(
            left - right
            for left, right in zip(self.values, other.values)
        )

    def detach(self):
        return self

    def float(self):
        return self

    def cpu(self):
        return self

    def tolist(self):
        return list(self.values)


class FakeLastTokenLogits:
    def __init__(self, scores):
        self.scores = list(scores)

    def __getitem__(self, key):
        _rows, token_id = key
        if token_id == 11:
            return FakeScoreVector(self.scores)
        if token_id == 12:
            return FakeScoreVector([0.0] * len(self.scores))
        raise AssertionError(f"unexpected token id {token_id}")


class FakeLogits:
    def __init__(self, scores):
        self.scores = list(scores)

    def __getitem__(self, key):
        assert key == (slice(None), -1, slice(None))
        return FakeLastTokenLogits(self.scores)


class FakeBatch(dict):
    def __init__(self, rows):
        super().__init__(input_ids=rows)
        self.device = None

    def to(self, device):
        self.device = device
        return self


class FakeTokenizer:
    def __init__(self, revision):
        self.init_kwargs = {"_commit_hash": revision}
        self.unk_token_id = 0
        self.pad_token_id = None
        self.pad_token = None
        self.eos_token = "<eos>"
        self.padding_side = "right"
        self.encoded_texts = []
        self.padded = []

    def encode(self, text, *, add_special_tokens):
        assert add_special_tokens is False
        self.encoded_texts.append(text)
        return [20, 21] if "<Instruct>:" not in text else [30, 31, 32]

    def convert_tokens_to_ids(self, token):
        return {"yes": 11, "no": 12}[token]

    def pad(
        self,
        features,
        *,
        padding,
        return_tensors,
        pad_to_multiple_of,
    ):
        assert padding is True
        assert return_tensors == "pt"
        assert pad_to_multiple_of == 8
        self.padded.append(features)
        return FakeBatch([item["input_ids"] for item in features])


class FakeOOM(RuntimeError):
    pass


class FakeCuda:
    OutOfMemoryError = FakeOOM

    def __init__(self):
        self.empty_cache_calls = 0

    def empty_cache(self):
        self.empty_cache_calls += 1


class FakeTorch:
    __version__ = "fake-torch"

    def __init__(self):
        self.cuda = FakeCuda()

    @staticmethod
    def inference_mode():
        return nullcontext()


class FakeModel:
    def __init__(
        self,
        revision,
        *,
        oom_above=None,
        always_oom=False,
        dtype="unknown",
    ):
        self.config = SimpleNamespace(_commit_hash=revision)
        self.oom_above = oom_above
        self.always_oom = always_oom
        self.dtype = dtype
        self.batch_sizes = []

    def __call__(self, **batch):
        size = len(batch["input_ids"])
        self.batch_sizes.append(size)
        if self.always_oom or (
            self.oom_above is not None and size > self.oom_above
        ):
            raise FakeOOM("CUDA out of memory")
        return SimpleNamespace(
            logits=FakeLogits(
                [float(index + 1) for index in range(size)]
            )
        )


def _fake_loader(*, revision=models.RERANKER_REVISION, **model_kwargs):
    calls = []
    torch = FakeTorch()
    tokenizer = FakeTokenizer(revision)
    model = FakeModel(revision, **model_kwargs)

    def loader(model_id, actual_revision, hf_home, device, local_files_only):
        calls.append(
            (
                model_id,
                actual_revision,
                hf_home,
                device,
                local_files_only,
            )
        )
        return torch, tokenizer, model

    return loader, calls, torch, tokenizer, model


def test_reranker_is_lazy_and_preflight_binds_hf_home_revision_and_device(
    tmp_path,
):
    loader, calls, _torch, tokenizer, _model = _fake_loader()
    adapter = models.Qwen3RerankerTransformersAdapter(
        hf_home=tmp_path / "hf",
        device="cuda:0",
        local_files_only=True,
        component_loader=loader,
    )

    assert calls == []
    preflight = adapter.preflight()

    assert calls == [
        (
            models.RERANKER_MODEL_ID,
            models.RERANKER_REVISION,
            (tmp_path / "hf").resolve(),
            "cuda:0",
            True,
        )
    ]
    assert preflight.revision == models.RERANKER_REVISION
    assert preflight.source.endswith("@" + models.RERANKER_REVISION)
    assert len(preflight.fingerprint) == 64
    assert tokenizer.padding_side == "left"
    assert tokenizer.pad_token == tokenizer.eos_token
    assert adapter.preflight() is preflight
    assert len(calls) == 1


def test_reranker_formats_query_passage_and_returns_raw_stable_scores(tmp_path):
    loader, _calls, _torch, tokenizer, model = _fake_loader()
    adapter = models.Qwen3RerankerTransformersAdapter(
        hf_home=tmp_path,
        component_loader=loader,
    )

    scores = adapter.score_pairs(
        "what is alpha?",
        ("alpha passage", "beta passage"),
        batch_size=8,
    )

    formatted = [
        text
        for text in tokenizer.encoded_texts
        if "<Instruct>:" in text
    ]
    assert formatted == [
        (
            f"<Instruct>: {models.RERANKER_INSTRUCTION}\n"
            "<Query>: what is alpha?\n"
            "<Document>: alpha passage"
        ),
        (
            f"<Instruct>: {models.RERANKER_INSTRUCTION}\n"
            "<Query>: what is alpha?\n"
            "<Document>: beta passage"
        ),
    ]
    assert scores == (1.0, 2.0)
    assert model.batch_sizes == [2]
    assert adapter.last_effective_batch_size == 2


def test_reranker_oom_only_reduces_batch_size_down_to_one(tmp_path):
    loader, calls, torch, _tokenizer, model = _fake_loader(oom_above=1)
    adapter = models.Qwen3RerankerTransformersAdapter(
        hf_home=tmp_path,
        device="cuda:7",
        component_loader=loader,
    )

    scores = adapter.score_pairs(
        "query",
        ("p1", "p2", "p3", "p4"),
        batch_size=4,
    )

    assert scores == (1.0, 1.0, 1.0, 1.0)
    assert model.batch_sizes == [4, 2, 1, 1, 1, 1]
    assert adapter.last_effective_batch_size == 1
    assert torch.cuda.empty_cache_calls == 2
    assert [call[3] for call in calls] == ["cuda:7"]


def test_reranker_oom_at_batch_one_fails_without_cpu_fallback(tmp_path):
    loader, calls, _torch, _tokenizer, model = _fake_loader(always_oom=True)
    adapter = models.Qwen3RerankerTransformersAdapter(
        hf_home=tmp_path,
        device="cuda",
        component_loader=loader,
    )

    with pytest.raises(
        models.ModelInferenceError,
        match="OOM at batch size 1",
    ):
        adapter.score_pairs("query", ("p1", "p2"), batch_size=2)

    assert model.batch_sizes == [2, 1]
    assert [call[3] for call in calls] == ["cuda"]


def test_reranker_preflight_rejects_resolved_revision_mismatch(tmp_path):
    loader, _calls, _torch, _tokenizer, _model = _fake_loader(
        revision="0" * 40
    )
    adapter = models.Qwen3RerankerTransformersAdapter(
        hf_home=tmp_path,
        component_loader=loader,
    )

    with pytest.raises(
        models.ModelPreflightError,
        match="resolved revision mismatch",
    ):
        adapter.preflight()


def test_reranker_release_is_idempotent_and_reloads_identical_preflight(
    tmp_path,
):
    loader, calls, torch, _tokenizer, _model = _fake_loader()
    adapter = models.Qwen3RerankerTransformersAdapter(
        hf_home=tmp_path,
        component_loader=loader,
    )
    first = adapter.preflight()

    assert adapter.release_model() is True
    assert adapter._torch is None
    assert adapter._tokenizer is None
    assert adapter._model is None
    assert torch.cuda.empty_cache_calls == 1
    assert adapter.release_model() is False
    assert torch.cuda.empty_cache_calls == 1

    second = adapter.preflight()

    assert second is first
    assert len(calls) == 2


def test_reranker_reload_rejects_identity_drift(tmp_path):
    calls = []
    dtypes = iter(("float16", "float32"))

    def loader(model_id, revision, hf_home, device, local_files_only):
        calls.append((model_id, revision, hf_home, device, local_files_only))
        return (
            FakeTorch(),
            FakeTokenizer(revision),
            FakeModel(revision, dtype=next(dtypes)),
        )

    adapter = models.Qwen3RerankerTransformersAdapter(
        hf_home=tmp_path,
        component_loader=loader,
    )
    first = adapter.preflight()
    adapter.release_model()

    with pytest.raises(models.ModelPreflightError, match="identity changed"):
        adapter.preflight()

    assert adapter._preflight is first
    assert adapter._model is None
    assert len(calls) == 2


def test_default_transformers_loader_pins_revision_cache_and_device(
    tmp_path,
    monkeypatch,
):
    calls = []

    class Factory:
        def __init__(self, value):
            self.value = value

        def from_pretrained(self, model_id, **kwargs):
            calls.append((model_id, kwargs))
            return self.value

    class LoadedModel:
        def __init__(self):
            self.to_calls = []
            self.eval_calls = 0

        def to(self, device):
            self.to_calls.append(device)
            return self

        def eval(self):
            self.eval_calls += 1
            return self

    tokenizer = object()
    model = LoadedModel()
    fake_torch = object()
    fake_transformers = SimpleNamespace(
        AutoTokenizer=Factory(tokenizer),
        AutoModelForCausalLM=Factory(model),
    )

    def fake_import(name):
        return {
            "torch": fake_torch,
            "transformers": fake_transformers,
        }[name]

    monkeypatch.setattr(models.importlib, "import_module", fake_import)
    loaded = models._load_transformers_components(
        models.RERANKER_MODEL_ID,
        models.RERANKER_REVISION,
        tmp_path,
        "cuda:3",
        True,
    )

    assert loaded == (fake_torch, tokenizer, model)
    assert calls[0] == (
        models.RERANKER_MODEL_ID,
        {
            "revision": models.RERANKER_REVISION,
            "cache_dir": str(tmp_path),
            "local_files_only": True,
            "padding_side": "left",
            "trust_remote_code": False,
        },
    )
    assert calls[1] == (
        models.RERANKER_MODEL_ID,
        {
            "revision": models.RERANKER_REVISION,
            "cache_dir": str(tmp_path),
            "local_files_only": True,
            "trust_remote_code": False,
            "use_safetensors": True,
        },
    )
    assert model.to_calls == ["cuda:3"]
    assert model.eval_calls == 1
