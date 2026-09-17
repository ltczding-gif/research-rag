"""Generation-scoped embedding identity; no network/model effects at import.

Ollama uses a unique verified copy alias; FastEmbed captures a loaded model.
External administrators must not mutate session-owned aliases or loaded model
objects. Remote compatible APIs retain operator-declared assurance, not weight
attestation. Session resources are closed before a generation is published.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import inspect
import json
import math
import time
from typing import Callable, Mapping, Sequence
from urllib.parse import urlsplit
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4


BUILD_SESSION_POLICY = "provider-bound-build-session-v1"


class EmbeddingIdentityError(ValueError):
    """The expected embedding contract no longer matches the observed provider."""


class OllamaRequestError(RuntimeError):
    """An Ollama HTTP failure with its status and daemon-provided detail."""

    def __init__(self, status: int, detail: str):
        self.status, self.detail = status, detail
        super().__init__(f'Ollama HTTP {status}: {detail}')


def _fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _vector(raw: Sequence[float], dimensions: int) -> list[float]:
    result, problem = _vector_problem(raw, dimensions)
    if problem:
        raise ValueError("Embedding does not match the declared dimensions or finite/nonzero contract")
    return result


def _vector_problem(raw: Sequence[float], dimensions: int):
    try:
        result = [float(value) for value in raw]
    except (TypeError, ValueError):
        return None, {'kind': 'type', 'dimensions': None, 'finite': False, 'nonzero': False}
    finite, nonzero = all(math.isfinite(value) for value in result), any(result)
    if len(result) != dimensions:
        return result, {'kind': 'dimensions', 'dimensions': len(result),
                        'finite': finite, 'nonzero': nonzero}
    if not finite or not nonzero:
        return result, {'kind': 'numeric', 'dimensions': len(result),
                        'finite': finite, 'nonzero': nonzero}
    return result, None


def _reader(function: Callable, dimensions: int) -> Callable:
    # Production identity checks can skip repeated billable dimension probes.
    try:
        supported = 'dimensions' in inspect.signature(function).parameters
    except (ValueError, TypeError):
        supported = False
    return (lambda: function(dimensions=dimensions)) if supported else function


class CheckedEmbeddingSession:
    """Observed before/after checks for injected adapters; not remote attestation."""

    assurance = "observed-adapter-contract"

    def __init__(self, expected: Mapping, contract_fn: Callable,
                 embed_fn: Callable, split_fn: Callable):
        self.expected = deepcopy(dict(expected))
        self.dimensions = self.expected.get('dimensions')
        if not isinstance(self.dimensions, int) or isinstance(self.dimensions, bool) or self.dimensions <= 0:
            raise ValueError('Embedding contract requires positive dimensions')
        self._expected_hash = _fingerprint(self.expected)
        self._contract = _reader(contract_fn, self.dimensions)
        self._embed, self._split = embed_fn, split_fn
        self._entered = self._closed = self._finished = False
        self._count = 0
        self._inputs = hashlib.sha256()
        self._returned_vectors = hashlib.sha256()
        self.cleanup_warnings: list[str] = []

    def check_source(self) -> None:
        if _fingerprint(self._contract()) != self._expected_hash:
            raise EmbeddingIdentityError('Embedding contract changed during this build')

    def _usable(self) -> None:
        if not self._entered or self._closed or self._finished:
            raise RuntimeError('Embedding session is not open for new inputs')

    def __enter__(self):
        if self._entered or self._closed:
            raise RuntimeError('Embedding sessions cannot be re-entered')
        self.check_source()
        self._entered = True
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc is None and not self._finished:
                self.finish()
        finally:
            self._closed = True
        return False

    def _record(self, text: str, raw: Sequence[float]) -> list[float]:
        vector = _vector(raw, self.dimensions)
        data = text.encode('utf-8')
        self._inputs.update(len(data).to_bytes(8, 'big'))
        self._inputs.update(data)
        # Record adapter-returned Python floats, not a claim about stored Chroma
        # bytes. This helps distinguish runtime numerical drift from input drift.
        for value in vector:
            encoded = value.hex().encode('ascii')
            self._returned_vectors.update(len(encoded).to_bytes(8, 'big'))
            self._returned_vectors.update(encoded)
        self._count += 1
        return vector

    def embed(self, text: str) -> list[float]:
        self._usable()
        if not isinstance(text, str) or not text:
            raise ValueError('Embedding input must be nonempty text')
        self.check_source()
        raw = self._embed(text)
        self.check_source()
        return self._record(text, raw)

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed inputs in order; providers may override with a true batch request."""
        if not isinstance(texts, Sequence) or isinstance(texts, (str, bytes)) or not texts:
            raise ValueError('Embedding batch must contain at least one text input')
        return [self.embed(text) for text in texts]

    def split(self, text: str):
        self._usable()
        self.check_source()
        pieces = self._split(text)
        self.check_source()
        return pieces

    def finish(self) -> None:
        self._usable()
        self.check_source()
        self._finished = True

    def summary(self) -> dict:
        if not self._finished or not self._closed:
            raise RuntimeError('Cannot publish an unfinished embedding-session receipt')
        return {
            'schema_version': 1, 'policy': BUILD_SESSION_POLICY, 'assurance': self.assurance,
            'source_contract_fingerprint': self._expected_hash,
            # Kept for existing consumers: this counts embedded inputs, not transport calls.
            'request_count': self._count, 'request_count_unit': 'input',
            'ordered_inputs_sha256': self._inputs.hexdigest(),
            'returned_vectors_float_hex_sha256': self._returned_vectors.hexdigest(),
            'cleanup_warnings': list(self.cleanup_warnings), 'bitwise_reproducibility_claim': False,
        }


def _split_by_end(text: str, maximum: int, input_end: Callable):
    if maximum <= 0:
        raise ValueError('Maximum embedding characters must be positive')
    result, start = [], 0
    while start < len(text):
        candidate = text[start:start + maximum]
        length = input_end(candidate)
        if not 0 < length <= len(candidate):
            raise ValueError('Bound embedding tokenizer cannot represent this input')
        end = start + length
        result.append((start, end, text[start:end]))
        start = end
    return result


class FastEmbedBuildSession(CheckedEmbeddingSession):
    assurance = 'captured-local-model-instance'

    def __init__(self, expected, contract_fn, provider):
        self.provider = provider
        self.model = provider._get_fastembed_model()
        self.tokenizer = self.model.model.tokenizer
        self.maximum = int(expected['max_embed_chars'])
        super().__init__(expected, contract_fn, self._bound_embed, self._bound_split)

    def _input_end(self, text):
        end = min(len(text), self.maximum)
        encoded = self.tokenizer.encode(text[:end])
        retained = max((stop for start, stop in encoded.offsets if stop > start), default=0)
        return retained if encoded.overflowing or text[retained:end].strip() else end

    def check_source(self):
        super().check_source()
        if self.provider._get_fastembed_model() is not self.model:
            raise EmbeddingIdentityError('The loaded FastEmbed model instance changed')
        if self.model.model.tokenizer is not self.tokenizer:
            raise EmbeddingIdentityError('The bound FastEmbed tokenizer instance changed')

    def _bound_embed(self, text):
        if self._input_end(text) != len(text):
            raise ValueError('Embedding input would be truncated; split it before indexing')
        return next(iter(self.model.embed([text])))

    def _bound_split(self, text):
        return _split_by_end(text, self.maximum, self._input_end)


class OllamaTransport:
    """The existing local-daemon HTTP contract, with normal TLS verification."""

    def __init__(self, base_url: str, timeout: float = 180):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError('Ollama endpoint must be HTTP(S) without embedded credentials/query')
        self.base, self.timeout = base_url.rstrip('/'), timeout

    def request(self, method: str, path: str, payload=None):
        data = json.dumps(payload, allow_nan=False).encode() if payload is not None else None
        request = Request(self.base + path, data=data, method=method,
                          headers={'Content-Type': 'application/json'})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raw_error = exc.read()
            try:
                detail = json.loads(raw_error).get('error', '')
            except (ValueError, AttributeError):
                detail = ''
            raise OllamaRequestError(exc.code, str(detail)[:500] or str(exc.reason)) from exc
        value = json.loads(raw) if raw.strip() else {}
        if not isinstance(value, dict):
            raise ValueError('Ollama returned a non-object response')
        return value


def _model_name(name: str) -> str:
    return name if ':' in name.rsplit('/', 1)[-1] else name + ':latest'


class OllamaBuildSession(CheckedEmbeddingSession):
    """Snapshot a source tag under an owned alias; never modify the source tag.

    This isolates ordinary concurrent pulls of the source. An administrator who
    mutates this session's alias is outside the contract; digest polling is not
    cryptographic per-response weight attestation.
    """
    assurance = 'owned-copy-alias-with-digest-checks'

    def __init__(self, expected, contract_fn, *, transport=None):
        import re
        endpoint = str(expected.get('endpoint', ''))
        if not endpoint.endswith('/api/embed'):
            raise ValueError('Ollama build sessions require /api/embed')
        revision = expected.get('revision')
        if not isinstance(revision, str) or not re.fullmatch(r'[a-f0-9]{64}', revision):
            raise ValueError('Ollama build sessions require a manifest digest')
        context, maximum = expected.get('model_context_tokens'), expected.get('max_embed_chars')
        if (not isinstance(context, int) or isinstance(context, bool) or context <= 16
                or not isinstance(maximum, int) or isinstance(maximum, bool) or maximum <= 0):
            raise ValueError('Ollama build sessions require positive input limits')
        # Match the existing service's conservative Qwen input-size policy.
        self.maximum = min(maximum, max(1, (context - 16) // 4))
        self.source_model = str(expected['model'])
        self.runtime_model = 'research-rag-build-' + uuid4().hex + ':latest'
        self.transport = transport or OllamaTransport(endpoint[:-len('/api/embed')])
        self._embedding_http_request_count = 0
        self._transient_retry_count = 0
        self._invalid_vector_retry_count = 0
        self._copied = False
        super().__init__(expected, contract_fn, self._bound_embed, self._bound_split)

    def _digest(self, model):
        rows = self.transport.request('GET', '/api/tags').get('models')
        if not isinstance(rows, list):
            raise EmbeddingIdentityError('Ollama model inventory is unavailable')
        matches = [row for row in rows if isinstance(row, dict) and any(
            isinstance(row.get(field), str) and _model_name(row[field]) == _model_name(model)
            for field in ('name', 'model'))]
        if not matches:
            return None
        if len(matches) != 1 or not isinstance(matches[0].get('digest'), str):
            raise EmbeddingIdentityError('Ollama model identity is not unique')
        return matches[0]['digest']

    def _check_runtime(self):
        if self._digest(self.runtime_model) != self.expected['revision']:
            raise EmbeddingIdentityError('The session-owned Ollama alias changed or disappeared')

    def __enter__(self):
        super().__enter__()
        try:
            if self._digest(self.runtime_model) is not None:
                raise EmbeddingIdentityError('Refusing to overwrite an existing model alias')
            try:
                self.transport.request('POST', '/api/copy', {
                    'source': self.source_model, 'destination': self.runtime_model})
            except Exception as exc:
                raise EmbeddingIdentityError(
                    f'Copy outcome unknown for owned alias {self.runtime_model}; inspect daemon before cleanup'
                ) from exc
            self._copied = True
            self._check_runtime()
            return self
        except BaseException:
            self._cleanup()
            self._closed = True
            raise

    def _bound_embed(self, text):
        return self._bound_embed_batch([text])[0]

    def _bound_embed_batch(self, texts: Sequence[str]):
        if any(len(text) > self.maximum for text in texts):
            raise ValueError('Embedding input would be truncated; split it before indexing')
        for attempt in range(3):
            self._check_runtime()
            self._embedding_http_request_count += 1
            try:
                response = self.transport.request('POST', '/api/embed', {
                    'model': self.runtime_model, 'input': list(texts), 'truncate': False})
            except OllamaRequestError as exc:
                transient = any(marker in exc.detail.lower() for marker in (
                    'runner connection reset', 'connection reset', 'forcibly closed',
                    'unexpected eof', 'connection refused',
                ))
                if not transient or attempt == 2:
                    raise
                self._transient_retry_count += 1
                time.sleep(1)
                continue
            if (not isinstance(response.get('model'), str)
                    or _model_name(response['model']) != self.runtime_model):
                raise EmbeddingIdentityError('Ollama response belongs to a different runtime model')
            vectors = response.get('embeddings')
            if not isinstance(vectors, list) or len(vectors) != len(texts):
                raise ValueError('Ollama returned the wrong number of embeddings')
            self._check_runtime()
            problems = [(index, problem) for index, raw in enumerate(vectors)
                        if (problem := _vector_problem(raw, self.dimensions)[1])]
            if not problems:
                return vectors
            index, problem = next((entry for entry in problems if entry[1]['kind'] != 'numeric'),
                                  problems[0])
            detail = (f"Ollama returned invalid embedding at index {index}: "
                      f"observed_dimensions={problem['dimensions']}, "
                      f"finite={problem['finite']}, nonzero={problem['nonzero']}")
            if problem['kind'] != 'numeric' or attempt == 2:
                raise ValueError(detail)
            self._invalid_vector_retry_count += 1
            time.sleep(1)
        raise AssertionError('unreachable Ollama embedding retry state')

    def embed(self, text):
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        self._usable()
        if not isinstance(texts, Sequence) or isinstance(texts, (str, bytes)) or not texts:
            raise ValueError('Embedding batch must contain at least one text input')
        values = list(texts)
        if any(not isinstance(text, str) or not text for text in values):
            raise ValueError('Embedding input must be nonempty text')
        # Every input goes to the captured alias even if the source tag changes.
        return [self._record(text, raw) for text, raw in zip(values, self._bound_embed_batch(values), strict=True)]

    def _bound_split(self, text):
        return _split_by_end(text, self.maximum, len)

    def split(self, text):
        self._usable()
        return self._bound_split(text)

    def finish(self):
        self._usable()
        self.check_source()  # The published generation must match serving configuration.
        self._check_runtime()
        self._finished = True

    def _cleanup(self):
        if not self._copied:
            return
        try:
            self._check_runtime()
            self.transport.request('DELETE', '/api/delete', {'model': self.runtime_model})
            self._copied = False
        except Exception as exc:
            self.cleanup_warnings.append(
                f'Owned alias {self.runtime_model} was not removed: {type(exc).__name__}: {exc}')

    def __exit__(self, exc_type, exc, tb):
        try:
            return super().__exit__(exc_type, exc, tb)
        finally:
            self._cleanup()

    def summary(self):
        return {**super().summary(), 'embedding_http_request_count': self._embedding_http_request_count,
                'transient_retry_count': self._transient_retry_count,
                'invalid_vector_retry_count': self._invalid_vector_retry_count,
                'source_model': self.source_model,
                'runtime_model': self.runtime_model,
                'limitation': 'External mutation of owned alias is unsupported; no per-response weight attestation.'}


class RemoteDeclaredBuildSession(CheckedEmbeddingSession):
    assurance = 'operator-declared-remote-deployment'

    def __init__(self, expected, contract_fn, split_fn, provider):
        if expected.get('revision_kind') != 'operator_declared' or not expected.get('revision'):
            raise EmbeddingIdentityError('Remote builds require an operator-pinned deployment revision')
        self.endpoint, self.model = str(expected['endpoint']), str(expected['model'])
        self.api_key = provider.OPENAI_EMBED_API_KEY
        if not self.api_key:
            raise ValueError('Remote embedding API key is missing')
        self.maximum = int(expected['max_embed_chars'])
        super().__init__(expected, contract_fn, self._bound_embed, split_fn)

    def _bound_embed(self, text):
        if len(text) > self.maximum:
            raise ValueError('Embedding input would be truncated; split it before indexing')
        request = Request(self.endpoint, data=json.dumps({'model': self.model, 'input': text}).encode(),
                          headers={'Content-Type': 'application/json', 'Authorization': f'Bearer {self.api_key}'},
                          method='POST')
        with urlopen(request, timeout=180) as response:
            payload = json.loads(response.read())
        rows = payload.get('data')
        if not isinstance(rows, list) or len(rows) != 1:
            raise ValueError('Remote provider returned the wrong number of embeddings')
        return rows[0]['embedding']


def create_build_embedding_session(expected, contract_fn, embed_fn, split_fn):
    """Construct a lazy provider session; close it before publishing active."""
    name = expected.get('provider')
    if name == 'ollama':
        return OllamaBuildSession(expected, contract_fn)
    if name in {'fastembed', 'openai-compat'}:
        try:
            from . import embedding_client as provider
        except ImportError:
            import embedding_client as provider
        if name == 'fastembed':
            return FastEmbedBuildSession(expected, contract_fn, provider)
        return RemoteDeclaredBuildSession(expected, contract_fn, split_fn, provider)
    return CheckedEmbeddingSession(expected, contract_fn, embed_fn, split_fn)
