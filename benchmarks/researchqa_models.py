"""Strict live-model adapters for the ResearchQA rq-2 benchmark.

Imports are offline-safe: neither Torch nor Transformers is imported until the
reranker is first preflighted or used.  The Ollama client performs no retries;
transient retry policy belongs to the overnight runner.
"""

from __future__ import annotations

import gc
import hashlib
import importlib
import json
import math
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence

from benchmarks.researchqa_retrieval import (
    RERANKER_MODEL_ID,
    RERANKER_REVISION,
    embedding_cache_key,
    text_sha256,
)


OLLAMA_EMBED_MODEL_ID = "qwen3-embedding:4b"
OLLAMA_EMBED_MODEL_DIGEST = (
    "df5bd2e3c74cd8d069d21dc038f1b359fcdc9458fce1c99bd43c9eb1518ff907"
)
OLLAMA_EMBED_DIMENSIONS = 2560
OLLAMA_EMBED_ENDPOINT = "/api/embed"
OLLAMA_GENERATE_ENDPOINT = "/api/generate"
OLLAMA_TAGS_ENDPOINT = "/api/tags"
DEFAULT_NORMALIZATION_REVISION = "exact-text-utf8-v1"
MODEL_ADAPTER_REVISION = "researchqa-model-adapters-v2"
RERANKER_MAX_LENGTH = 8192
RERANKER_INSTRUCTION = (
    "Given a web search query, retrieve relevant passages that answer the query"
)
_RERANKER_PREFIX = (
    "<|im_start|>system\n"
    "Judge whether the Document meets the requirements based on the Query and "
    'the Instruct provided. Note that the answer can only be "yes" or "no".'
    "<|im_end|>\n"
    "<|im_start|>user\n"
)
_RERANKER_SUFFIX = (
    "<|im_end|>\n"
    "<|im_start|>assistant\n"
    "<think>\n\n</think>\n\n"
)


class ModelAdapterError(RuntimeError):
    """Base error for strict live-model adapter failures."""


class ModelPreflightError(ModelAdapterError):
    """Raised when a pinned model identity cannot be proven."""


class ModelTransportError(ModelAdapterError):
    """Raised for one failed HTTP request; the adapter never retries it."""


class ModelResponseError(ModelAdapterError):
    """Raised when a live model returns malformed or mismatched output."""


class ModelCacheError(ModelAdapterError):
    """Raised when a cache artifact cannot be trusted."""


class ModelInferenceError(ModelAdapterError):
    """Raised when fixed-model inference cannot complete."""


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _fingerprint(value: object) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _require_finite_vector(
    raw_vector: object,
    *,
    dimensions: int,
    label: str,
) -> tuple[float, ...]:
    if not isinstance(raw_vector, list):
        raise ModelResponseError(f"{label} must be an array")
    if len(raw_vector) != dimensions:
        raise ModelResponseError(
            f"{label} has {len(raw_vector)} dimensions; expected {dimensions}"
        )
    try:
        vector = tuple(float(value) for value in raw_vector)
    except (TypeError, ValueError) as exc:
        raise ModelResponseError(f"{label} contains a non-numeric value") from exc
    if not all(math.isfinite(value) for value in vector):
        raise ModelResponseError(f"{label} contains a non-finite value")
    return vector


@dataclass(frozen=True)
class ModelPreflight:
    """Hashable, reader-facing proof of one live model identity."""

    provider: str
    model_id: str
    revision: str
    source: str
    dimensions: int | None
    adapter_revision: str
    fingerprint: str
    details: Mapping[str, object]

    def to_dict(self) -> dict[str, object]:
        value = asdict(self)
        value["details"] = dict(sorted(self.details.items()))
        return value


class JsonTransport(Protocol):
    """Minimal injectable HTTP boundary used by the Ollama adapter."""

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, object] | None,
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        """Execute exactly one request and return a decoded JSON object."""


class UrllibJsonTransport:
    """Strict standard-library JSON transport with normal TLS verification."""

    def request_json(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, object] | None,
        timeout_seconds: float,
    ) -> Mapping[str, object]:
        data = _canonical_json_bytes(payload) if payload is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            method=method,
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=timeout_seconds,
            ) as response:
                raw = response.read()
        except (
            TimeoutError,
            urllib.error.HTTPError,
            urllib.error.URLError,
            OSError,
        ) as exc:
            raise ModelTransportError(
                f"{method} {url} failed: {exc}"
            ) from exc
        try:
            decoded = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelResponseError(
                f"{method} {url} returned invalid JSON"
            ) from exc
        if not isinstance(decoded, dict):
            raise ModelResponseError(
                f"{method} {url} must return a JSON object"
            )
        return decoded


def _validated_ollama_base_url(base_url: str) -> str:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Ollama base_url must be an http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("Ollama base_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("Ollama base_url must not contain query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError(
            "Ollama base_url must not contain an API path; "
            "the adapter fixes /api/embed and /api/tags"
        )
    return base_url.rstrip("/")


class OllamaBatchEmbeddingClient:
    """Pinned batch `/api/embed` client with per-text atomic disk caching."""

    model_id = OLLAMA_EMBED_MODEL_ID
    model_digest = OLLAMA_EMBED_MODEL_DIGEST
    dimensions = OLLAMA_EMBED_DIMENSIONS

    def __init__(
        self,
        *,
        cache_dir: str | Path,
        base_url: str = "http://localhost:11434",
        normalization_revision: str = DEFAULT_NORMALIZATION_REVISION,
        timeout_seconds: float = 60.0,
        transport: JsonTransport | None = None,
    ) -> None:
        if not normalization_revision:
            raise ValueError("normalization_revision must be non-empty")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than zero")
        self.base_url = _validated_ollama_base_url(base_url)
        self.cache_dir = Path(cache_dir).resolve(strict=False)
        self.normalization_revision = normalization_revision
        self.timeout_seconds = float(timeout_seconds)
        self.transport = transport or UrllibJsonTransport()
        self._preflight: ModelPreflight | None = None
        self._cache_only = False
        self._model_loaded = False
        self.last_cache_hits = 0
        self.last_cache_misses = 0

    @property
    def embed_url(self) -> str:
        return self.base_url + OLLAMA_EMBED_ENDPOINT

    @property
    def tags_url(self) -> str:
        return self.base_url + OLLAMA_TAGS_ENDPOINT

    @property
    def generate_url(self) -> str:
        return self.base_url + OLLAMA_GENERATE_ENDPOINT

    def _request(
        self,
        method: str,
        url: str,
        *,
        payload: Mapping[str, object] | None,
    ) -> Mapping[str, object]:
        try:
            response = self.transport.request_json(
                method,
                url,
                payload=payload,
                timeout_seconds=self.timeout_seconds,
            )
        except (ModelAdapterError, KeyboardInterrupt):
            raise
        except Exception as exc:
            raise ModelTransportError(
                f"{method} {url} failed: {exc}"
            ) from exc
        if not isinstance(response, Mapping):
            raise ModelResponseError(
                f"{method} {url} must return a JSON object"
            )
        return response

    def _model_record(self) -> Mapping[str, object]:
        payload = self._request("GET", self.tags_url, payload=None)
        records = payload.get("models")
        if not isinstance(records, list):
            raise ModelPreflightError(
                "Ollama /api/tags response is missing models[]"
            )
        matches = [
            record
            for record in records
            if isinstance(record, Mapping)
            and (
                record.get("name") == self.model_id
                or record.get("model") == self.model_id
            )
        ]
        if not matches:
            raise ModelPreflightError(
                f"Ollama model {self.model_id!r} is not installed"
            )
        digests = {record.get("digest") for record in matches}
        if digests != {self.model_digest}:
            raise ModelPreflightError(
                f"Ollama model digest mismatch for {self.model_id}: "
                f"expected {self.model_digest}, found {sorted(map(str, digests))}"
            )
        return matches[0]

    def _post_embed(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        self._model_loaded = True
        response = self._request(
            "POST",
            self.embed_url,
            payload={
                "model": self.model_id,
                "input": list(texts),
                "truncate": False,
            },
        )
        if response.get("model") != self.model_id:
            raise ModelResponseError(
                f"Ollama /api/embed returned model {response.get('model')!r}; "
                f"expected {self.model_id!r}"
            )
        raw_embeddings = response.get("embeddings")
        if not isinstance(raw_embeddings, list):
            raise ModelResponseError(
                "Ollama /api/embed response is missing embeddings[]"
            )
        if len(raw_embeddings) != len(texts):
            raise ModelResponseError(
                "Ollama /api/embed returned "
                f"{len(raw_embeddings)} vectors for {len(texts)} inputs"
            )
        return tuple(
            _require_finite_vector(
                vector,
                dimensions=self.dimensions,
                label=f"embedding[{index}]",
            )
            for index, vector in enumerate(raw_embeddings)
        )

    def preflight(self) -> ModelPreflight:
        """Prove the installed digest and live response dimensionality."""
        if self._preflight is not None:
            return self._preflight
        record = self._model_record()
        probe = self._post_embed(("research-rag rq2 dimension probe",))[0]
        identity = {
            "provider": "ollama",
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "dimensions": len(probe),
            "model_source": self.tags_url,
            "endpoint": OLLAMA_EMBED_ENDPOINT,
            "normalization_revision": self.normalization_revision,
            "adapter_revision": MODEL_ADAPTER_REVISION,
        }
        details = record.get("details")
        self._preflight = ModelPreflight(
            provider="ollama",
            model_id=self.model_id,
            revision=self.model_digest,
            source=self.tags_url,
            dimensions=len(probe),
            adapter_revision=MODEL_ADAPTER_REVISION,
            fingerprint=_fingerprint(identity),
            details={
                "endpoint": OLLAMA_EMBED_ENDPOINT,
                "normalization_revision": self.normalization_revision,
                "record_name": str(record.get("name", "")),
                "record_model": str(record.get("model", "")),
                "record_digest": str(record.get("digest", "")),
                "record_details_sha256": _fingerprint(
                    details if isinstance(details, Mapping) else {}
                ),
            },
        )
        return self._preflight

    def enter_cache_only(self) -> None:
        """Forbid network-backed embedding after one successful preflight."""
        if self._preflight is None:
            raise ModelPreflightError(
                "cache-only mode requires a successful model preflight"
            )
        self._cache_only = True

    def release_model(self) -> bool:
        """Unload this client's fixed Ollama model, at most once per load."""
        if not self._model_loaded:
            return False
        response = self._request(
            "POST",
            self.generate_url,
            payload={
                "model": self.model_id,
                "keep_alive": 0,
            },
        )
        expected = {
            "model": self.model_id,
            "response": "",
            "done": True,
            "done_reason": "unload",
        }
        mismatches = {
            key: response.get(key)
            for key, value in expected.items()
            if (
                response.get(key) is not True
                if key == "done"
                else response.get(key) != value
            )
        }
        created_at = response.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            mismatches["created_at"] = created_at
        if mismatches:
            raise ModelResponseError(
                "Ollama unload response mismatch: "
                f"expected {expected}, found {mismatches}"
            )
        self._model_loaded = False
        return True

    def _cache_key(self, text: str) -> str:
        return embedding_cache_key(
            model_digest=self.model_digest,
            normalization_revision=self.normalization_revision,
            text=text,
        )

    def _cache_path(self, cache_key: str) -> Path:
        artifact_id = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
        return self.cache_dir / artifact_id[:2] / f"{artifact_id}.json"

    def _read_cache(
        self,
        path: Path,
        *,
        cache_key: str,
        text: str,
    ) -> tuple[float, ...]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ModelCacheError(f"invalid embedding cache artifact: {path}") from exc
        expected = {
            "schema_version": 1,
            "cache_key": cache_key,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "normalization_revision": self.normalization_revision,
            "text_sha256": text_sha256(text),
            "dimensions": self.dimensions,
        }
        if not isinstance(payload, dict) or any(
            payload.get(key) != value for key, value in expected.items()
        ):
            raise ModelCacheError(
                f"embedding cache metadata mismatch: {path}"
            )
        try:
            return _require_finite_vector(
                payload.get("embedding"),
                dimensions=self.dimensions,
                label=f"cached embedding {path}",
            )
        except ModelResponseError as exc:
            raise ModelCacheError(str(exc)) from exc

    def _write_cache(
        self,
        path: Path,
        *,
        cache_key: str,
        text: str,
        embedding: Sequence[float],
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": 1,
            "cache_key": cache_key,
            "model_id": self.model_id,
            "model_digest": self.model_digest,
            "normalization_revision": self.normalization_revision,
            "text_sha256": text_sha256(text),
            "dimensions": self.dimensions,
            "embedding": list(embedding),
        }
        descriptor, raw_temp_path = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temp_path = Path(raw_temp_path)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(_canonical_json_bytes(payload))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
        finally:
            temp_path.unlink(missing_ok=True)

    def embed_texts(
        self,
        texts: Sequence[str],
    ) -> tuple[tuple[float, ...], ...]:
        """Embed an ordered batch, requesting only cache misses."""
        if not texts:
            self.last_cache_hits = 0
            self.last_cache_misses = 0
            return ()
        if any(not isinstance(text, str) or not text for text in texts):
            raise ValueError("embedding inputs must be non-empty strings")
        if not self._cache_only:
            self.preflight()

        keys = tuple(self._cache_key(text) for text in texts)
        vectors_by_key: dict[str, tuple[float, ...]] = {}
        missing_by_key: dict[str, str] = {}
        cache_hits = 0
        for text, cache_key in zip(texts, keys):
            if cache_key in vectors_by_key or cache_key in missing_by_key:
                continue
            path = self._cache_path(cache_key)
            if path.is_file():
                vectors_by_key[cache_key] = self._read_cache(
                    path,
                    cache_key=cache_key,
                    text=text,
                )
                cache_hits += 1
            else:
                missing_by_key[cache_key] = text

        self.last_cache_hits = cache_hits
        self.last_cache_misses = len(missing_by_key)
        if missing_by_key and self._cache_only:
            raise ModelCacheError(
                "cache-only embedding miss for "
                f"{len(missing_by_key)} unique input(s)"
            )
        if missing_by_key:
            missing_keys = tuple(missing_by_key)
            missing_texts = tuple(missing_by_key.values())
            requested = self._post_embed(missing_texts)
            for cache_key, text, vector in zip(
                missing_keys,
                missing_texts,
                requested,
                strict=True,
            ):
                self._write_cache(
                    self._cache_path(cache_key),
                    cache_key=cache_key,
                    text=text,
                    embedding=vector,
                )
                vectors_by_key[cache_key] = vector

        return tuple(vectors_by_key[cache_key] for cache_key in keys)


TransformersComponentLoader = Callable[
    [str, str, Path, str, bool],
    tuple[object, object, object],
]


def _load_transformers_components(
    model_id: str,
    revision: str,
    hf_home: Path,
    device: str,
    local_files_only: bool,
) -> tuple[object, object, object]:
    """Late import and load the exact pinned CausalLM implementation."""
    try:
        torch = importlib.import_module("torch")
        transformers = importlib.import_module("transformers")
    except ImportError as exc:
        raise ModelPreflightError(
            "Qwen3 reranking requires the opt-in live benchmark dependencies"
        ) from exc
    tokenizer = transformers.AutoTokenizer.from_pretrained(
        model_id,
        revision=revision,
        cache_dir=str(hf_home),
        local_files_only=local_files_only,
        padding_side="left",
        trust_remote_code=False,
    )
    model_kwargs = {
        "revision": revision,
        "cache_dir": str(hf_home),
        "local_files_only": local_files_only,
        "trust_remote_code": False,
        "use_safetensors": True,
    }
    if device.casefold().startswith("cuda"):
        model_kwargs["dtype"] = torch.float16
    model = transformers.AutoModelForCausalLM.from_pretrained(
        model_id,
        **model_kwargs,
    )
    model.to(device)
    model.eval()
    return torch, tokenizer, model


def _resolved_hf_commit(tokenizer: object, model: object) -> str | None:
    config = getattr(model, "config", None)
    candidates = [
        getattr(config, "_commit_hash", None),
        getattr(tokenizer, "_commit_hash", None),
    ]
    init_kwargs = getattr(tokenizer, "init_kwargs", None)
    if isinstance(init_kwargs, Mapping):
        candidates.append(init_kwargs.get("_commit_hash"))
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _format_reranker_pair(query: str, passage: str) -> str:
    return (
        f"<Instruct>: {RERANKER_INSTRUCTION}\n"
        f"<Query>: {query}\n"
        f"<Document>: {passage}"
    )


class Qwen3RerankerTransformersAdapter:
    """Pinned Transformers adapter returning raw yes-minus-no logits."""

    model_id = RERANKER_MODEL_ID
    revision = RERANKER_REVISION

    def __init__(
        self,
        *,
        hf_home: str | Path,
        device: str = "cuda",
        max_length: int = RERANKER_MAX_LENGTH,
        local_files_only: bool = False,
        component_loader: TransformersComponentLoader | None = None,
    ) -> None:
        if not str(hf_home):
            raise ValueError("hf_home must be provided by the caller")
        if not device:
            raise ValueError("device must be non-empty")
        if max_length <= 0:
            raise ValueError("max_length must be greater than zero")
        self.hf_home = Path(hf_home).resolve(strict=False)
        self.device = device
        self.max_length = int(max_length)
        self.local_files_only = bool(local_files_only)
        self._component_loader = (
            component_loader or _load_transformers_components
        )
        self._torch: object | None = None
        self._tokenizer: object | None = None
        self._model: object | None = None
        self._prefix_tokens: tuple[int, ...] = ()
        self._suffix_tokens: tuple[int, ...] = ()
        self._true_token_id: int | None = None
        self._false_token_id: int | None = None
        self._preflight: ModelPreflight | None = None
        self.last_effective_batch_size: int | None = None

    def _load(self) -> None:
        if self._model is not None:
            return
        self.hf_home.mkdir(parents=True, exist_ok=True)
        torch, tokenizer, model = self._component_loader(
            self.model_id,
            self.revision,
            self.hf_home,
            self.device,
            self.local_files_only,
        )
        encode = getattr(tokenizer, "encode", None)
        convert = getattr(tokenizer, "convert_tokens_to_ids", None)
        if not callable(encode) or not callable(convert):
            raise ModelPreflightError(
                "reranker tokenizer lacks encode/token-id methods"
            )
        prefix_tokens = tuple(
            int(item)
            for item in encode(
                _RERANKER_PREFIX,
                add_special_tokens=False,
            )
        )
        suffix_tokens = tuple(
            int(item)
            for item in encode(
                _RERANKER_SUFFIX,
                add_special_tokens=False,
            )
        )
        true_token_id = convert("yes")
        false_token_id = convert("no")
        if (
            not isinstance(true_token_id, int)
            or not isinstance(false_token_id, int)
            or true_token_id < 0
            or false_token_id < 0
            or true_token_id == false_token_id
        ):
            raise ModelPreflightError(
                "reranker yes/no token IDs are invalid"
            )
        unknown_id = getattr(tokenizer, "unk_token_id", None)
        if true_token_id == unknown_id or false_token_id == unknown_id:
            raise ModelPreflightError(
                "reranker yes/no token resolves to the unknown token"
            )
        if not prefix_tokens or not suffix_tokens:
            raise ModelPreflightError(
                "reranker prefix/suffix tokenization is empty"
            )
        if len(prefix_tokens) + len(suffix_tokens) >= self.max_length:
            raise ModelPreflightError(
                "reranker max_length cannot fit the fixed prompt"
            )
        if getattr(tokenizer, "pad_token_id", None) is None:
            eos_token = getattr(tokenizer, "eos_token", None)
            if eos_token is None:
                raise ModelPreflightError(
                    "reranker tokenizer has neither pad nor EOS token"
                )
            tokenizer.pad_token = eos_token
        tokenizer.padding_side = "left"

        resolved_commit = _resolved_hf_commit(tokenizer, model)
        if resolved_commit != self.revision:
            raise ModelPreflightError(
                "reranker resolved revision mismatch: "
                f"expected {self.revision}, found {resolved_commit!r}"
            )
        self._torch = torch
        self._tokenizer = tokenizer
        self._model = model
        self._prefix_tokens = prefix_tokens
        self._suffix_tokens = suffix_tokens
        self._true_token_id = true_token_id
        self._false_token_id = false_token_id

    def preflight(self) -> ModelPreflight:
        """Load the exact revision and return its deterministic fingerprint."""
        if self._preflight is not None and self._model is not None:
            return self._preflight
        self._load()
        assert self._torch is not None
        assert self._tokenizer is not None
        assert self._model is not None
        model_dtype = str(getattr(self._model, "dtype", "unknown"))
        identity = {
            "provider": "huggingface-transformers",
            "model_id": self.model_id,
            "revision": self.revision,
            "device": self.device,
            "model_dtype": model_dtype,
            "max_length": self.max_length,
            "instruction_sha256": text_sha256(RERANKER_INSTRUCTION),
            "score": "last-token-yes-minus-no-logit",
            "truncation": "formatted-input-tail",
            "true_token_id": self._true_token_id,
            "false_token_id": self._false_token_id,
            "adapter_revision": MODEL_ADAPTER_REVISION,
        }
        candidate = ModelPreflight(
            provider="huggingface-transformers",
            model_id=self.model_id,
            revision=self.revision,
            source=f"huggingface://{self.model_id}@{self.revision}",
            dimensions=None,
            adapter_revision=MODEL_ADAPTER_REVISION,
            fingerprint=_fingerprint(identity),
            details={
                "device": self.device,
                "hf_home": str(self.hf_home),
                "max_length": self.max_length,
                "model_dtype": model_dtype,
                "local_files_only": self.local_files_only,
                "resolved_commit": self.revision,
                "tokenizer_class": type(self._tokenizer).__name__,
                "model_class": type(self._model).__name__,
                "transformers_version": str(
                    getattr(
                        importlib.import_module("transformers")
                        if self._component_loader
                        is _load_transformers_components
                        else object(),
                        "__version__",
                        "injected",
                    )
                ),
                "torch_version": str(
                    getattr(self._torch, "__version__", "injected")
                ),
            },
        )
        if (
            self._preflight is not None
            and candidate.to_dict() != self._preflight.to_dict()
        ):
            expected = self._preflight.fingerprint
            found = candidate.fingerprint
            self.release_model()
            raise ModelPreflightError(
                "reranker identity changed after reload: "
                f"expected {expected}, found {found}"
            )
        if self._preflight is None:
            self._preflight = candidate
        return self._preflight

    def release_model(self) -> bool:
        """Drop loaded reranker components while preserving proven identity."""
        had_components = any(
            value is not None
            for value in (self._torch, self._tokenizer, self._model)
        )
        if not had_components:
            return False
        torch = self._torch
        model = self._model
        tokenizer = self._tokenizer
        self._torch = None
        self._tokenizer = None
        self._model = None
        self._prefix_tokens = ()
        self._suffix_tokens = ()
        self._true_token_id = None
        self._false_token_id = None
        self.last_effective_batch_size = None
        del model
        del tokenizer
        gc.collect()
        cuda = getattr(torch, "cuda", None)
        empty_cache = getattr(cuda, "empty_cache", None)
        if callable(empty_cache):
            empty_cache()
        del torch
        return True

    def _batch_features(
        self,
        query: str,
        passages: Sequence[str],
    ) -> object:
        assert self._tokenizer is not None
        max_body_length = (
            self.max_length
            - len(self._prefix_tokens)
            - len(self._suffix_tokens)
        )
        features = []
        for passage in passages:
            body_tokens = tuple(
                int(item)
                for item in self._tokenizer.encode(
                    _format_reranker_pair(query, passage),
                    add_special_tokens=False,
                )
            )
            input_ids = (
                self._prefix_tokens
                + body_tokens[:max_body_length]
                + self._suffix_tokens
            )
            features.append({"input_ids": list(input_ids)})
        try:
            batch = self._tokenizer.pad(
                features,
                padding=True,
                return_tensors="pt",
                pad_to_multiple_of=8,
            )
        except Exception as exc:
            raise ModelInferenceError(
                f"reranker tokenization/padding failed: {exc}"
            ) from exc
        to_device = getattr(batch, "to", None)
        if callable(to_device):
            return to_device(self.device)
        if isinstance(batch, Mapping):
            moved = {}
            for key, value in batch.items():
                move = getattr(value, "to", None)
                moved[key] = move(self.device) if callable(move) else value
            return moved
        raise ModelInferenceError("reranker tokenizer returned an invalid batch")

    def _score_batch(
        self,
        query: str,
        passages: Sequence[str],
    ) -> tuple[float, ...]:
        assert self._torch is not None
        assert self._model is not None
        assert self._true_token_id is not None
        assert self._false_token_id is not None
        batch = self._batch_features(query, passages)
        inference_mode = getattr(self._torch, "inference_mode", None)
        if not callable(inference_mode):
            inference_mode = getattr(self._torch, "no_grad", None)
        if not callable(inference_mode):
            raise ModelInferenceError(
                "torch lacks inference_mode/no_grad"
            )
        with inference_mode():
            outputs = self._model(**batch)
        try:
            last_token_logits = outputs.logits[:, -1, :]
            scores_tensor = (
                last_token_logits[:, self._true_token_id]
                - last_token_logits[:, self._false_token_id]
            )
            raw_scores = (
                scores_tensor.detach().float().cpu().tolist()
            )
            scores = tuple(float(score) for score in raw_scores)
        except Exception as exc:
            raise ModelInferenceError(
                f"reranker output decoding failed: {exc}"
            ) from exc
        if len(scores) != len(passages):
            raise ModelInferenceError(
                "reranker returned the wrong number of scores"
            )
        if not all(math.isfinite(score) for score in scores):
            raise ModelInferenceError(
                "reranker returned a non-finite score"
            )
        return scores

    def _score_with_batch_size(
        self,
        query: str,
        passages: Sequence[str],
        *,
        batch_size: int,
    ) -> tuple[float, ...]:
        scores = []
        for start in range(0, len(passages), batch_size):
            scores.extend(
                self._score_batch(
                    query,
                    passages[start : start + batch_size],
                )
            )
        return tuple(scores)

    def _is_out_of_memory(self, exc: BaseException) -> bool:
        assert self._torch is not None
        cuda = getattr(self._torch, "cuda", None)
        oom_type = getattr(cuda, "OutOfMemoryError", None)
        if isinstance(oom_type, type) and isinstance(exc, oom_type):
            return True
        return "out of memory" in str(exc).casefold()

    def _clear_cuda_cache(self) -> None:
        assert self._torch is not None
        cuda = getattr(self._torch, "cuda", None)
        empty_cache = getattr(cuda, "empty_cache", None)
        if callable(empty_cache):
            empty_cache()

    def score_pairs(
        self,
        query: str,
        passages: Sequence[str],
        *,
        batch_size: int,
    ) -> Sequence[float]:
        """Score all pairs; on OOM only the batch size is reduced."""
        if not isinstance(query, str) or not query:
            raise ValueError("query must be a non-empty string")
        if any(not isinstance(passage, str) or not passage for passage in passages):
            raise ValueError("passages must contain non-empty strings")
        if batch_size <= 0:
            raise ValueError("batch_size must be greater than zero")
        if not passages:
            self.last_effective_batch_size = 0
            return ()
        self.preflight()
        effective_batch_size = min(batch_size, len(passages))
        while True:
            try:
                scores = self._score_with_batch_size(
                    query,
                    passages,
                    batch_size=effective_batch_size,
                )
                self.last_effective_batch_size = effective_batch_size
                return scores
            except Exception as exc:
                if not self._is_out_of_memory(exc):
                    raise
                if effective_batch_size == 1:
                    raise ModelInferenceError(
                        "reranker OOM at batch size 1; "
                        "model, device, and candidate depth were not changed"
                    ) from exc
                self._clear_cuda_cache()
                effective_batch_size = max(1, effective_batch_size // 2)


__all__ = [
    "DEFAULT_NORMALIZATION_REVISION",
    "MODEL_ADAPTER_REVISION",
    "ModelAdapterError",
    "ModelCacheError",
    "ModelInferenceError",
    "ModelPreflight",
    "ModelPreflightError",
    "ModelResponseError",
    "ModelTransportError",
    "OLLAMA_EMBED_DIMENSIONS",
    "OLLAMA_EMBED_ENDPOINT",
    "OLLAMA_EMBED_MODEL_DIGEST",
    "OLLAMA_EMBED_MODEL_ID",
    "OLLAMA_GENERATE_ENDPOINT",
    "OllamaBatchEmbeddingClient",
    "Qwen3RerankerTransformersAdapter",
    "RERANKER_INSTRUCTION",
    "RERANKER_MAX_LENGTH",
    "UrllibJsonTransport",
]
