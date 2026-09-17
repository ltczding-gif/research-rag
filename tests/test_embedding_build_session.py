"""Offline provider/session regression tests; never call a model or network."""
from __future__ import annotations

from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def sessions():
    path = Path(__file__).resolve().parents[1] / 'service' / 'embedding_session.py'
    spec = importlib.util.spec_from_file_location('embedding_session_under_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def expected(provider='fake'):
    return {'provider': provider, 'model': 'source:latest', 'revision': 'a'*64,
            'dimensions': 2, 'endpoint': 'http://localhost:11434/api/embed',
            'model_context_tokens': 8192, 'max_embed_chars': 12000}


def split(text):
    return [(0, len(text), text)]


def test_ollama_http_failure_keeps_actionable_error_body(sessions, monkeypatch):
    def failed(*args, **kwargs):
        raise sessions.HTTPError('http://localhost/api/embed', 400, 'Bad Request', {},
                                 io.BytesIO(b'{"error":"input exceeds the runtime context"}'))
    monkeypatch.setattr(sessions, 'urlopen', failed)
    with pytest.raises(RuntimeError, match='Ollama HTTP 400: input exceeds the runtime context'):
        sessions.OllamaTransport('http://localhost').request('POST', '/api/embed', {'input': ['text']})


def test_session_lifecycle_and_input_receipt(sessions):
    contract, probes = expected(), []
    def read(*, dimensions=None):
        probes.append(dimensions)
        return contract
    session = sessions.CheckedEmbeddingSession(contract, read, lambda _: [1, .5], split)
    with session:
        assert session.embed('first') == [1., .5]
        assert session.split('second') == [(0, 6, 'second')]
        session.embed('second')
    receipt = session.summary()
    assert receipt['request_count'] == 2 and len(receipt['ordered_inputs_sha256']) == 64
    assert receipt['bitwise_reproducibility_claim'] is False
    assert set(probes) == {2}
    with pytest.raises(RuntimeError):
        session.embed('closed')
    with pytest.raises(RuntimeError):
        session.__enter__()


@pytest.mark.parametrize('when', ['enter', 'before', 'during', 'finish'])
def test_observed_drift_never_gets_completed_receipt(sessions, when):
    contract = expected()
    current = deepcopy(contract)
    def embed(text):
        if when == 'during':
            current['revision'] = 'b'*64
        return [1, .5]
    session = sessions.CheckedEmbeddingSession(contract, lambda: current, embed, split)
    if when == 'enter':
        current['revision'] = 'b'*64
    with pytest.raises(sessions.EmbeddingIdentityError):
        with session:
            if when == 'before':
                current['revision'] = 'b'*64
            session.embed('input')
            if when == 'finish':
                current['revision'] = 'b'*64
    with pytest.raises(RuntimeError):
        session.summary()


@pytest.mark.parametrize('vector', [[], [0, 0], [1], [float('nan'), 1], [float('inf'), 0]])
def test_invalid_vector_cannot_count_as_complete(sessions, vector):
    contract = expected()
    session = sessions.CheckedEmbeddingSession(contract, lambda: contract, lambda _: vector, split)
    with pytest.raises(ValueError):
        with session:
            session.embed('input')
    with pytest.raises(RuntimeError):
        session.summary()


@pytest.mark.parametrize('dimension', [None, 0, -1, True, '2'])
def test_invalid_dimension_fails_preflight(sessions, dimension):
    contract = {**expected(), 'dimensions': dimension}
    with pytest.raises(ValueError):
        sessions.CheckedEmbeddingSession(contract, lambda: contract, lambda _: [1, .5], split)


class Daemon:
    """Fake registry: copy snapshots a digest and inference follows that alias."""
    def __init__(self):
        self.models = {'source:latest': 'a'*64}
        self.calls, self.inferred = [], []
        self.before_copy = self.after_embed = self.delete_error = None
        self.response_model = self.copy_error = self.response_embeddings = None
        self.embed_errors = []

    def request(self, method, path, payload=None):
        self.calls.append((method, path, deepcopy(payload)))
        if path == '/api/tags':
            return {'models': [{'name': name, 'digest': digest} for name, digest in self.models.items()]}
        if path == '/api/copy':
            if self.before_copy:
                self.before_copy(self, payload)
            self.models[payload['destination']] = self.models[payload['source']]
            if self.copy_error:
                raise self.copy_error
            return {}
        if path == '/api/embed':
            assert payload['truncate'] is False
            digest = self.models[payload['model']]
            self.inferred.append(digest)
            if self.embed_errors:
                raise self.embed_errors.pop(0)
            if self.after_embed:
                self.after_embed(self, payload)
            embeddings = (self.response_embeddings(payload['input'], digest)
                          if self.response_embeddings else
                          [[1. if digest == 'a'*64 else 2., .5] for _ in payload['input']])
            return {'model': self.response_model or payload['model'],
                    'embeddings': embeddings}
        if path == '/api/delete':
            if self.delete_error:
                raise self.delete_error
            del self.models[payload['model']]
            return {}
        raise AssertionError((method, path, payload))


def ollama(sessions):
    daemon = Daemon()
    contract = expected('ollama')
    def read():
        return {**contract, 'revision': daemon.models['source:latest']}
    return sessions.OllamaBuildSession(contract, read, transport=daemon), daemon


def test_ollama_uses_own_alias_and_only_deletes_own_alias(sessions):
    session, daemon = ollama(sessions)
    assert not daemon.calls
    with session:
        session.embed('first')
        session.embed('second')
    assert daemon.models == {'source:latest': 'a'*64}
    inputs = [body['model'] for _, path, body in daemon.calls if path == '/api/embed']
    deletes = [body['model'] for _, path, body in daemon.calls if path == '/api/delete']
    assert inputs == [session.runtime_model]*2
    assert deletes == [session.runtime_model]
    assert session.summary()['request_count'] == 2
    assert not session.summary()['cleanup_warnings']


def test_source_a_b_a_still_uses_one_snapshot(sessions):
    session, daemon = ollama(sessions)
    with session:
        session.embed('A')
        daemon.models['source:latest'] = 'b'*64
        session.embed('while source is B')
        daemon.models['source:latest'] = 'a'*64
        session.embed('source restored')
    assert daemon.inferred == ['a'*64]*3


def test_source_changed_at_finish_blocks_serving_incompatible_generation(sessions):
    session, daemon = ollama(sessions)
    with pytest.raises(sessions.EmbeddingIdentityError):
        with session:
            session.embed('input')
            daemon.models['source:latest'] = 'b'*64
    assert session.runtime_model not in daemon.models
    assert daemon.models['source:latest'] == 'b'*64
    with pytest.raises(RuntimeError):
        session.summary()


def test_alias_collision_is_never_overwritten(sessions):
    session, daemon = ollama(sessions)
    daemon.models[session.runtime_model] = 'a'*64
    with pytest.raises(sessions.EmbeddingIdentityError):
        with session:
            pytest.fail('collision')
    assert not [c for c in daemon.calls if c[1] in {'/api/copy', '/api/delete'}]


def test_source_race_during_copy_rejects_wrong_digest(sessions):
    session, daemon = ollama(sessions)
    daemon.before_copy = lambda d, body: d.models.update({'source:latest': 'b'*64})
    with pytest.raises(sessions.EmbeddingIdentityError):
        with session:
            pytest.fail('wrong digest')
    assert not daemon.inferred
    assert not [c for c in daemon.calls if c[1] == '/api/delete']
    assert session.cleanup_warnings


@pytest.mark.parametrize('when', ['before', 'after'])
def test_private_alias_mutation_is_detected_not_deleted(sessions, when):
    session, daemon = ollama(sessions)
    with pytest.raises(sessions.EmbeddingIdentityError):
        with session:
            if when == 'before':
                daemon.models[session.runtime_model] = 'b'*64
            else:
                daemon.after_embed = lambda d, body: d.models.update({body['model']: 'b'*64})
            session.embed('input')
    assert daemon.models[session.runtime_model] == 'b'*64
    assert not [c for c in daemon.calls if c[1] == '/api/delete']


def test_copy_timeout_records_unknown_target_without_guessing_deletion(sessions):
    session, daemon = ollama(sessions)
    daemon.copy_error = OSError('server completed but response lost')
    with pytest.raises(sessions.EmbeddingIdentityError, match=session.runtime_model):
        with session:
            pytest.fail('unknown copy')
    assert not daemon.inferred
    assert not [c for c in daemon.calls if c[1] == '/api/delete']


def test_response_model_mismatch_fails(sessions):
    session, daemon = ollama(sessions)
    daemon.response_model = 'other:latest'
    with pytest.raises(sessions.EmbeddingIdentityError, match='response'):
        with session:
            session.embed('input')


def test_ollama_batch_uses_one_request_and_records_returned_vectors_in_input_order(sessions):
    session, daemon = ollama(sessions)
    daemon.response_embeddings = lambda texts, _digest: [[float(len(text)), .5] for text in texts]
    texts = ['a', 'three', 'twenty']
    with session:
        assert session.embed_batch(texts) == [[1., .5], [5., .5], [6., .5]]
    requests = [body for _, path, body in daemon.calls if path == '/api/embed']
    assert len(requests) == 1 and requests[0]['input'] == texts
    receipt = session.summary()
    assert receipt['request_count'] == len(texts)
    assert receipt['request_count_unit'] == 'input'
    assert receipt['embedding_http_request_count'] == 1

    serial = sessions.CheckedEmbeddingSession(
        expected(), lambda: expected(), lambda text: [float(len(text)), .5], split,
    )
    with serial:
        for text in texts:
            serial.embed(text)
    reversed_serial = sessions.CheckedEmbeddingSession(
        expected(), lambda: expected(), lambda text: [float(len(text)), .5], split,
    )
    with reversed_serial:
        for text in reversed(texts):
            reversed_serial.embed(text)
    assert receipt['ordered_inputs_sha256'] == serial.summary()['ordered_inputs_sha256']
    assert receipt['ordered_inputs_sha256'] != reversed_serial.summary()['ordered_inputs_sha256']
    assert receipt['returned_vectors_float_hex_sha256'] == serial.summary()['returned_vectors_float_hex_sha256']


def test_ollama_batch_rejects_count_or_runtime_identity_mismatch(sessions):
    session, daemon = ollama(sessions)
    daemon.response_embeddings = lambda texts, _digest: [[1., .5]]
    with pytest.raises(ValueError, match='wrong number'):
        with session:
            session.embed_batch(['one', 'two'])


def test_ollama_batch_retries_only_a_transient_runner_disconnect(sessions, monkeypatch):
    session, daemon = ollama(sessions)
    daemon.embed_errors = [sessions.OllamaRequestError(
        400, 'do embedding request: runner connection reset by peer',
    )]
    pauses = []
    monkeypatch.setattr(sessions.time, 'sleep', pauses.append)
    with session:
        assert session.embed_batch(['one', 'two']) == [[1., .5], [1., .5]]
    receipt = session.summary()
    assert receipt['request_count'] == 2
    assert receipt['embedding_http_request_count'] == 2
    assert receipt['transient_retry_count'] == 1
    assert pauses == [1]


def test_ollama_batch_does_not_retry_invalid_input_or_exceed_retry_cap(sessions, monkeypatch):
    pauses = []
    monkeypatch.setattr(sessions.time, 'sleep', pauses.append)
    session, daemon = ollama(sessions)
    daemon.embed_errors = [sessions.OllamaRequestError(400, 'input exceeds the runtime context')]
    with pytest.raises(sessions.OllamaRequestError, match='input exceeds'):
        with session:
            session.embed_batch(['one', 'two'])
    assert len([call for call in daemon.calls if call[1] == '/api/embed']) == 1
    assert not pauses

    session, daemon = ollama(sessions)
    daemon.embed_errors = [sessions.OllamaRequestError(400, 'unexpected EOF')] * 3
    with pytest.raises(sessions.OllamaRequestError, match='unexpected EOF'):
        with session:
            session.embed_batch(['one', 'two'])
    assert len([call for call in daemon.calls if call[1] == '/api/embed']) == 3
    assert pauses == [1, 1]


def test_ollama_batch_retries_numeric_invalid_vectors_before_recording_inputs(sessions, monkeypatch):
    session, daemon = ollama(sessions)
    responses = [
        [[0., 0.], [float('nan'), .5]],
        [[1., .5], [1., .5]],
    ]
    daemon.response_embeddings = lambda texts, _digest: responses.pop(0)
    pauses = []
    monkeypatch.setattr(sessions.time, 'sleep', pauses.append)
    with session:
        assert session.embed_batch(['one', 'two']) == [[1., .5], [1., .5]]
    receipt = session.summary()
    assert receipt['request_count'] == 2
    assert receipt['embedding_http_request_count'] == 2
    assert receipt['invalid_vector_retry_count'] == 1
    assert pauses == [1]


def test_ollama_batch_caps_numeric_invalid_vector_retries_and_never_retries_wrong_dimensions(sessions, monkeypatch):
    pauses = []
    monkeypatch.setattr(sessions.time, 'sleep', pauses.append)
    session, daemon = ollama(sessions)
    daemon.response_embeddings = lambda texts, _digest: [[0., 0.] for _ in texts]
    with pytest.raises(ValueError, match='index 0.*observed_dimensions=2.*nonzero=False'):
        with session:
            session.embed_batch(['one', 'two'])
    assert len([call for call in daemon.calls if call[1] == '/api/embed']) == 3
    assert pauses == [1, 1]
    with pytest.raises(RuntimeError):
        session.summary()

    session, daemon = ollama(sessions)
    daemon.response_embeddings = lambda texts, _digest: [[1.] for _ in texts]
    with pytest.raises(ValueError, match='observed_dimensions=1'):
        with session:
            session.embed_batch(['one', 'two'])
    assert len([call for call in daemon.calls if call[1] == '/api/embed']) == 1

    session, daemon = ollama(sessions)
    daemon.after_embed = lambda d, body: d.models.update({body['model']: 'b'*64})
    with pytest.raises(sessions.EmbeddingIdentityError, match='alias'):
        with session:
            session.embed_batch(['one', 'two'])


def test_cleanup_problem_is_visible_without_false_failure(sessions):
    session, daemon = ollama(sessions)
    daemon.delete_error = OSError('daemon unavailable')
    with session:
        session.embed('input')
    assert session.summary()['cleanup_warnings']
    assert daemon.models['source:latest'] == 'a'*64


def test_cleanup_never_masks_primary_failure(sessions):
    session, daemon = ollama(sessions)
    daemon.delete_error = OSError('cleanup error')
    with pytest.raises(RuntimeError, match='primary'):
        with session:
            raise RuntimeError('primary')
    assert session.cleanup_warnings


def test_ollama_limits_and_contiguous_splitting(sessions):
    session, daemon = ollama(sessions)
    with session:
        text = '中a\n'*2000
        pieces = session.split(text)
        assert ''.join(p[2] for p in pieces) == text
        assert pieces[0][0] == 0 and pieces[-1][1] == len(text)
        assert all(a[1] == b[0] for a, b in zip(pieces, pieces[1:]))
        assert max(len(p[2]) for p in pieces) <= session.maximum
        with pytest.raises(ValueError, match='truncated'):
            session.embed('x'*(session.maximum+1))
    assert not daemon.inferred


@pytest.mark.parametrize('url', ['file:///tmp/file', 'https://u:p@example.com', 'https://example.com?q=secret', 'relative'])
def test_transport_rejects_unsupported_endpoints(sessions, url):
    with pytest.raises(ValueError):
        sessions.OllamaTransport(url)


class Tokenizer:
    def encode(self, text):
        return SimpleNamespace(offsets=[(0, min(len(text), 5))], overflowing=[])


class Model:
    def __init__(self):
        self.model = SimpleNamespace(tokenizer=Tokenizer())
        self.inputs = []
    def embed(self, texts):
        self.inputs.extend(texts)
        return [[1, .5] for _ in texts]


def test_fastembed_bound_instance_and_window(sessions):
    model, contract = Model(), expected('fastembed')
    provider = SimpleNamespace(_get_fastembed_model=lambda: model)
    session = sessions.FastEmbedBuildSession(contract, lambda: contract, provider)
    with session:
        pieces = session.split('abcdefghij')
        assert [p[2] for p in pieces] == ['abcde', 'fghij']
        for _, _, text in pieces:
            session.embed(text)
        with pytest.raises(ValueError, match='truncated'):
            session.embed('sixsix')
    assert model.inputs == ['abcde', 'fghij']


@pytest.mark.parametrize('what', ['model', 'tokenizer'])
def test_fastembed_replacement_is_not_silently_used(sessions, what):
    models, contract = [Model()], expected('fastembed')
    provider = SimpleNamespace(_get_fastembed_model=lambda: models[0])
    session = sessions.FastEmbedBuildSession(contract, lambda: contract, provider)
    with pytest.raises(sessions.EmbeddingIdentityError):
        with session:
            if what == 'model':
                models[0] = Model()
            else:
                models[0].model.tokenizer = Tokenizer()
            session.embed('word')


def test_remote_captures_endpoint_key_and_model_without_secret_in_receipt(sessions, monkeypatch):
    contract = {**expected('openai-compat'), 'endpoint': 'https://example.test/v1/embeddings',
                'revision_kind': 'operator_declared'}
    provider = SimpleNamespace(OPENAI_EMBED_API_KEY='old-secret')
    requests = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return json.dumps({'data': [{'embedding': [1, .5]}]}).encode()
    def request(value, **kwargs):
        requests.append(value)
        return Response()
    monkeypatch.setattr(sessions, 'urlopen', request)
    session = sessions.RemoteDeclaredBuildSession(contract, lambda: contract, split, provider)
    provider.OPENAI_EMBED_API_KEY = 'new-secret'
    with session:
        session.embed('input')
    assert requests[0].full_url == contract['endpoint']
    assert requests[0].get_header('Authorization') == 'Bearer old-secret'
    assert json.loads(requests[0].data)['model'] == contract['model']
    assert session.summary()['assurance'] == 'operator-declared-remote-deployment'
    assert 'secret' not in json.dumps(session.summary())


def test_receipt_separates_numerical_output_drift_from_identical_inputs(sessions):
    contract = expected()
    receipts = []
    for vector in ([1, .5], [1, .5000001]):
        session = sessions.CheckedEmbeddingSession(contract, lambda: contract, lambda _: vector, split)
        with session:
            session.embed('identical input')
        receipts.append(session.summary())
    assert receipts[0]['ordered_inputs_sha256'] == receipts[1]['ordered_inputs_sha256']
    assert receipts[0]['source_contract_fingerprint'] == receipts[1]['source_contract_fingerprint']
    assert receipts[0]['returned_vectors_float_hex_sha256'] != receipts[1]['returned_vectors_float_hex_sha256']
    assert all(not value['bitwise_reproducibility_claim'] for value in receipts)
