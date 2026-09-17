"""Real-file boundary tests; collection and model interfaces are controlled fakes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def generation_module():
    return load_module('generation_commit_under_test', 'service/index_generation.py')


class Collection:
    def __init__(self, metadata):
        self.metadata = dict(metadata)
        self.records = {}

    def count(self):
        return len(self.records)

    def upsert(self, *, ids, documents, embeddings, metadatas):
        for key, text, vector, metadata in zip(ids, documents, embeddings, metadatas, strict=True):
            self.records[key] = (text, vector, metadata)

    add = upsert

    def get(self, *, ids, include):
        return {'ids': [key for key in ids if key in self.records]}


class Client:
    def __init__(self):
        self.collections = {}

    def get_or_create_collection(self, *, name, metadata):
        if name not in self.collections:
            self.collections[name] = Collection(metadata)
        return self.collections[name]

    def create_collection(self, *, name, metadata):
        if name in self.collections:
            raise ValueError('collection exists')
        return self.get_or_create_collection(name=name, metadata=metadata)

    def get_collection(self, name):
        return self.collections[name]

    def list_collections(self):
        return list(self.collections)

    def delete_collection(self, name):
        del self.collections[name]


def candidate(store):
    item = store.begin({'embedding': {'model': 'fixed'}}, [
        {'source_id': 'one', 'content_hash': 'a' * 64, 'status': 'success'},
    ])
    (store.generation_path(item) / 'source.md').write_text('A complete source.', encoding='utf-8')
    return item


def publish(store, client):
    item = candidate(store)
    col = client.create_collection(name=item['collection_name'], metadata={'generation_id': item['generation_id']})
    col.records['one'] = ('text', [1.0], {})
    return store.publish(item, item_count=1, artifacts={'notes': {'one': 'source.md'}})


@pytest.mark.parametrize('failure', [KeyboardInterrupt, OSError, RuntimeError, SystemExit])
@pytest.mark.parametrize('boundary', ['before-seal', 'after-seal', 'before-latest', 'before-pointer', 'after-pointer'])
def test_publication_fault_matrix(generation_module, monkeypatch, tmp_path, boundary, failure):
    g = generation_module
    store = g.GenerationStore(tmp_path, 'notes')
    old = publish(store, Client())
    new = candidate(store)
    manifest_path = store.generation_path(new) / 'manifest.json'
    old_bytes = (store.generation_path(old) / 'manifest.json').read_bytes()
    write = g.atomic_write_json
    fired = False

    def inject(path, value):
        nonlocal fired
        path = Path(path)
        seal = path == manifest_path and value.get('state') == 'complete'
        latest = path.name == 'latest-attempt.json' and value.get('state') == 'complete'
        pointer = path.name == 'active.json'
        hit = ((boundary in {'before-seal', 'after-seal'} and seal)
               or (boundary == 'before-latest' and latest)
               or (boundary in {'before-pointer', 'after-pointer'} and pointer))
        if hit and not fired:
            fired = True
            if boundary.startswith('after-'):
                write(path, value)
            raise failure('injected')
        write(path, value)

    monkeypatch.setattr(g, 'atomic_write_json', inject)
    expected = g.PublicationCommittedError if boundary == 'after-pointer' else failure
    with pytest.raises(expected) as error:
        store.publish(new, item_count=1, artifacts={'notes': {'one': 'source.md'}})
    before_error_record = manifest_path.read_bytes()
    outcome = store.fail(new, error.value)
    active = store.load_active()
    assert fired
    assert active['generation_id'] == (new if boundary == 'after-pointer' else old)['generation_id']
    assert active['state'] == 'complete'
    if boundary == 'after-pointer':
        assert outcome == 'committed'
    if boundary != 'before-seal':
        assert manifest_path.read_bytes() == before_error_record
    assert (store.generation_path(old) / 'manifest.json').read_bytes() == old_bytes


def test_complete_manifest_is_immutable(generation_module, tmp_path):
    g = generation_module
    store = g.GenerationStore(tmp_path, 'notes')
    active = publish(store, Client())
    raw = (store.generation_path(active) / 'manifest.json').read_bytes()
    with pytest.raises(g.GenerationIntegrityError, match='immutable'):
        store._save_attempt({**active, 'state': 'failed'})
    assert (store.generation_path(active) / 'manifest.json').read_bytes() == raw


@pytest.mark.parametrize('sealed', [True, False])
def test_late_failure_does_not_overwrite_newer_attempt(generation_module, tmp_path, sealed):
    g = generation_module
    store = g.GenerationStore(tmp_path, 'notes')
    old = publish(store, Client()) if sealed else candidate(store)
    new = candidate(store)
    store.fail(old, OSError('late error'))
    assert store.latest_attempt()['generation_id'] == new['generation_id']


def test_error_log_disk_failure_cannot_poison_commit(generation_module, monkeypatch, tmp_path):
    g = generation_module
    store = g.GenerationStore(tmp_path, 'notes')
    active = publish(store, Client())
    raw = (store.generation_path(active) / 'manifest.json').read_bytes()
    def fail(*args):
        raise OSError('disk full')
    monkeypatch.setattr(g, 'atomic_write_json', fail)
    assert store.fail(active, RuntimeError('post-commit')) == 'committed'
    assert store.load_active() == active
    assert (store.generation_path(active) / 'manifest.json').read_bytes() == raw


@pytest.mark.parametrize('bad', [b'{broken', b'[]', b'null', b'{"generation_id":"../../escape"}'])
def test_explicit_recovery_preserves_bad_pointer_bytes(generation_module, tmp_path, bad):
    g = generation_module
    store, client = g.GenerationStore(tmp_path, 'notes'), Client()
    active = publish(store, client)
    raw = (store.generation_path(active) / 'manifest.json').read_bytes()
    (store.root / 'active.json').write_bytes(bad)
    with pytest.raises(g.GenerationIntegrityError):
        store.load_active()
    assert store.status(client)['recovery_required']
    with store.writer_lock():
        result = store.recover(client, active['generation_id'], active['contract']['embedding'],
                               expected_manifest_fingerprint=g.fingerprint(active))
    assert result == active == store.load_active()
    assert (store.generation_path(active) / 'manifest.json').read_bytes() == raw
    receipts = list((store.root / 'recovery').glob('*.json'))
    assert len(receipts) == 1
    receipt = json.loads(receipts[0].read_text())
    assert bytes.fromhex(receipt['previous_pointer_hex']) == bad
    assert receipt['previous_pointer_sha256'] == hashlib.sha256(bad).hexdigest()


@pytest.mark.parametrize('invalid', ['fingerprint', 'model', 'artifact', 'identity', 'count'])
def test_recovery_rejects_bad_target_without_pointer_change(generation_module, tmp_path, invalid):
    g = generation_module
    store, client = g.GenerationStore(tmp_path, 'notes'), Client()
    active = publish(store, client)
    pointer = store.root / 'active.json'
    pointer.write_text('{broken')
    trusted, model = g.fingerprint(active), active['contract']['embedding']
    if invalid == 'fingerprint':
        trusted = '0' * 64
    elif invalid == 'model':
        model = {'model': 'other'}
    elif invalid == 'artifact':
        (store.generation_path(active) / 'source.md').unlink()
    elif invalid == 'identity':
        client.get_collection(active['collection_name']).metadata['generation_id'] = 'other'
    else:
        client.get_collection(active['collection_name']).records.clear()
    with pytest.raises(ValueError):
        store.recover(client, active['generation_id'], model, expected_manifest_fingerprint=trusted)
    assert pointer.read_text() == '{broken'


def test_recovery_refuses_valid_active(generation_module, tmp_path):
    g = generation_module
    store, client = g.GenerationStore(tmp_path, 'notes'), Client()
    active = publish(store, client)
    with pytest.raises(ValueError, match='valid'):
        store.recover(client, active['generation_id'], active['contract']['embedding'],
                      expected_manifest_fingerprint=g.fingerprint(active))
    assert store.load_active() == active


def test_recovery_detects_external_pointer_change(generation_module, monkeypatch, tmp_path):
    g = generation_module
    store, client = g.GenerationStore(tmp_path, 'notes'), Client()
    active = publish(store, client)
    pointer = store.root / 'active.json'
    pointer.write_text('{broken')
    original = g.atomic_write_json
    def external_change(path, value):
        original(path, value)
        if Path(path).parent.name == 'recovery':
            pointer.write_text('external update')
    monkeypatch.setattr(g, 'atomic_write_json', external_change)
    with pytest.raises(g.GenerationIntegrityError, match='changed during'):
        store.recover(client, active['generation_id'], active['contract']['embedding'],
                      expected_manifest_fingerprint=g.fingerprint(active))
    assert pointer.read_text() == 'external update'


@pytest.mark.parametrize('operation', ['rollback', 'recover'])
def test_maintenance_postcommit_interruption_preserves_manifest(generation_module, monkeypatch, tmp_path, operation):
    g = generation_module
    store, client = g.GenerationStore(tmp_path, 'notes'), Client()
    first = publish(store, client)
    publish(store, client)
    if operation == 'recover':
        (store.root / 'active.json').write_text('{broken')
    write = g.atomic_write_json
    def after_write(path, value):
        write(path, value)
        if Path(path).name == 'active.json':
            raise KeyboardInterrupt('after replace')
    monkeypatch.setattr(g, 'atomic_write_json', after_write)
    with pytest.raises(g.PublicationCommittedError):
        if operation == 'rollback':
            store.rollback(client, first['contract']['embedding'])
        else:
            store.recover(client, first['generation_id'], first['contract']['embedding'],
                          expected_manifest_fingerprint=g.fingerprint(first))
    assert store.load_active() == first


@pytest.fixture
def notes_builder(generation_module, monkeypatch, tmp_path):
    config = ModuleType('config')
    for name, value in {'NOTES_DIR': tmp_path/'notes', 'CHROMA_PATH': tmp_path/'chroma',
                        'NOTES_COLLECTION_NAME': 'notes', 'NOTES_LEDGER': tmp_path/'ledger',
                        'NOTE_SUFFIX': '_review_note.md'}.items():
        setattr(config, name, value)
    embedding = ModuleType('embedding_client')
    embedding.embed_index_text = lambda text: [1., .5]
    embedding.get_embedding = embedding.embed_index_text
    embedding.split_embedding_text = lambda text: [(0, len(text), text)]
    embedding.embedding_contract = lambda: {'provider': 'fake', 'model': 'fixed', 'revision': 'A', 'dimensions': 2}
    client = Client()
    chroma = ModuleType('chromadb')
    chroma.PersistentClient = lambda **kwargs: client
    for name, value in {'config': config, 'embedding_client': embedding, 'chromadb': chroma,
                        'index_generation': generation_module,
                        'embedding_session': load_module('session_for_builder_tests', 'service/embedding_session.py')}.items():
        monkeypatch.setitem(sys.modules, name, value)
    builder = load_module('notes_commit_under_test', 'service/build_notes_db.py')
    config.NOTES_DIR.mkdir()
    (config.NOTES_DIR/'one_review_note.md').write_text(
        '---\nzotero_parent_key: PARENT01\n---\n# Findings\nComplete source.\n# Tail\nCondition.\n', encoding='utf-8')
    return builder, client, generation_module.GenerationStore(config.CHROMA_PATH, 'notes')


@pytest.mark.parametrize('damage', ['collection', 'record', 'identity', 'artifact-missing', 'artifact-changed'])
def test_notes_repair_same_input_damaged_generation(notes_builder, damage):
    builder, client, store = notes_builder
    old, reused = builder.build_notes_generation()
    assert not reused
    if damage == 'collection':
        del client.collections[old['collection_name']]
    elif damage == 'record':
        client.get_collection(old['collection_name']).records.popitem()
    elif damage == 'identity':
        client.get_collection(old['collection_name']).metadata['generation_id'] = 'wrong'
    else:
        artifact = store.generation_path(old)/next(iter(old['artifacts']['notes'].values()))
        if damage == 'artifact-missing':
            artifact.unlink()
        else:
            artifact.write_text('modified')
    repaired, reused = builder.build_notes_generation()
    assert not reused and repaired['generation_id'] != old['generation_id']
    assert store.load_active() == repaired
    assert store.generation_usable(client, repaired)


def test_healthy_reuse_and_force_rebuild_preserve_previous(notes_builder):
    builder, client, store = notes_builder
    first, reused = builder.build_notes_generation()
    again, reused = builder.build_notes_generation()
    assert reused and first == again
    rebuilt, reused = builder.build_notes_generation(rebuild=True)
    assert not reused and rebuilt['generation_id'] != first['generation_id']
    assert first['collection_name'] in client.collections
    assert store.load_active() == rebuilt


def test_notes_bad_manifest_not_silently_rebuilt(notes_builder):
    builder, client, store = notes_builder
    first, _ = builder.build_notes_generation()
    pointer = store.root / 'active.json'
    pointer.write_text('{broken')
    with pytest.raises(ValueError):
        builder.build_notes_generation(rebuild=True)
    assert pointer.read_text() == '{broken'
    assert first['collection_name'] in client.collections


@pytest.mark.parametrize('error', [KeyboardInterrupt, OSError])
def test_full_notes_builder_handles_postcommit_exception(notes_builder, generation_module, monkeypatch, error):
    builder, client, store = notes_builder
    old, _ = builder.build_notes_generation()
    original = generation_module.GenerationStore.publish
    def after_publish(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise error('after publication returned')
    monkeypatch.setattr(generation_module.GenerationStore, 'publish', after_publish)
    with pytest.raises(generation_module.PublicationCommittedError):
        builder.build_notes_generation(rebuild=True)
    active = store.load_active()
    assert active['generation_id'] != old['generation_id']
    assert store.generation_usable(client, active)


def test_rebuild_failure_keeps_old_active(notes_builder):
    builder, client, store = notes_builder
    old, _ = builder.build_notes_generation()
    def error(text):
        raise RuntimeError('provider unavailable')
    with pytest.raises(RuntimeError):
        builder.build_notes_generation(rebuild=True, embed_fn=error)
    assert store.load_active() == old
    assert store.latest_attempt()['state'] == 'failed'


@pytest.fixture
def pdf_builder(notes_builder, generation_module, monkeypatch):
    _, client, _ = notes_builder
    config = sys.modules['config']
    for key, value in {'PAPERS_COLLECTION_NAME': 'papers', 'PDF_LEDGER': config.CHROMA_PATH/'pdf-ledger',
                       'ZOTERO_DB_PATH': config.CHROMA_PATH/'zotero.sqlite', 'EMBED_PROVIDER': 'fake',
                       'CHUNK_SIZE': 800, 'CHUNK_STEP': 700, 'MIN_CHUNK_LEN': 100}.items():
        setattr(config, key, value)
    baseline = ModuleType('pdf_baseline')
    baseline.chunk_text = lambda text: [text]
    baseline.extract_text_pdfplumber = lambda path: 'text'
    source = SimpleNamespace(file_id='Main', status='success')
    source.to_dict = lambda: {'source_id': 'one', 'content_hash': 'a'*64, 'status': 'success'}
    plan = SimpleNamespace(inventory=[source], documents=[], publishable=True,
        chunks=[SimpleNamespace(chunk_id=f'chunk-{i}', file_id='Main', text=f'source {i}') for i in range(3)])
    sources = ModuleType('pdf_sources')
    sources.PdfSourceError = ValueError
    sources.PreparedPdfBuild = SimpleNamespace
    sources.prepare_pdf_build = lambda *args, **kwargs: plan
    sources.split_prepared_chunks_for_embedding = lambda value, splitter: value
    sources.resolve_attachment_identity_exact = lambda *args: None
    sources.canonical_chunk_start = lambda *args: 0
    monkeypatch.setitem(sys.modules, 'pdf_baseline', baseline)
    monkeypatch.setitem(sys.modules, 'pdf_sources', sources)
    builder = load_module('pdf_commit_under_test', 'service/build_pdf_db.py')
    monkeypatch.setattr(builder, '_validate_prepared_build', lambda value: None)
    monkeypatch.setattr(builder, '_build_contract', lambda plan, contract, implementation: {'embedding': contract()})
    monkeypatch.setattr(builder, '_page_records', lambda plan, gen: [{'generation_id': gen, 'text': 'source'}])
    monkeypatch.setattr(builder, '_chunk_metadata', lambda chunk, source, gen: {'generation_id': gen})
    return builder, client, generation_module.GenerationStore(config.CHROMA_PATH, 'papers')


@pytest.mark.parametrize('error', [KeyboardInterrupt, OSError])
def test_full_pdf_cli_after_active_write_returns_committed_warning(pdf_builder, generation_module, monkeypatch, error, capsys):
    builder, client, store = pdf_builder
    assert builder.main([]) == 0
    old = store.load_active()
    write = generation_module.atomic_write_json
    fired = False
    def after_write(path, value):
        nonlocal fired
        write(path, value)
        if Path(path) == store.root/'active.json' and not fired:
            fired = True
            raise error('after active replacement')
    monkeypatch.setattr(generation_module, 'atomic_write_json', after_write)
    assert builder.main(['--rebuild']) == 3
    active = store.load_active()
    assert active['generation_id'] != old['generation_id'] and store.generation_usable(client, active)
    output = capsys.readouterr()
    assert 'COMMITTED WITH WARNING' in output.err
    assert 'active generation unchanged' not in output.out + output.err


def test_notes_stdout_failure_does_not_claim_failed_build(notes_builder, monkeypatch):
    import builtins
    builder, client, store = notes_builder
    def broken(*args, **kwargs):
        if args and str(args[0]).startswith('[DONE]'):
            raise BrokenPipeError('downstream closed')
        builtins.print(*args, **kwargs)
    monkeypatch.setattr(builder, 'print', broken, raising=False)
    assert builder.main([]) == 3
    assert store.generation_usable(client, store.load_active())


def test_pdf_stdout_failure_does_not_poison_manifest(pdf_builder, monkeypatch):
    import builtins
    builder, client, store = pdf_builder
    def broken(*args, **kwargs):
        if args and str(args[0]).startswith('[OK]'):
            raise BrokenPipeError('downstream closed')
        builtins.print(*args, **kwargs)
    monkeypatch.setattr(builder, 'print', broken, raising=False)
    assert builder.main([]) == 3
    assert store.generation_usable(client, store.load_active())


def test_orchestrator_stops_after_committed_warning(capsys):
    module = load_module('build_indexes_under_test', 'scripts/build_indexes.py')
    commands = module.build_commands('python', rebuild_notes=True, rebuild_papers=True)
    calls = []
    def runner(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=3)
    assert module.run_builds(commands, runner=runner) == 3
    assert len(calls) == 1 and '--rebuild' in calls[0]
    assert 'COMMITTED WITH WARNING' in capsys.readouterr().err


@pytest.mark.parametrize('location', ['generation', 'generation-root', 'pointer'])
def test_recovery_refuses_symlink_escape(generation_module, tmp_path, location):
    g = generation_module
    store, client = g.GenerationStore(tmp_path/'chroma', 'notes'), Client()
    active = publish(store, client)
    generation = store.generation_path(active)
    pointer = store.root/'active.json'
    pointer.write_text('{broken')
    external = tmp_path/'external'
    try:
        if location == 'generation':
            generation.rename(external)
            generation.symlink_to(external, target_is_directory=True)
        elif location == 'generation-root':
            (store.root/'generations').rename(external)
            (store.root/'generations').symlink_to(external, target_is_directory=True)
        else:
            pointer.rename(external)
            pointer.symlink_to(external)
    except OSError:
        pytest.skip('Creating test symlinks is not supported by this OS/account')
    with pytest.raises(g.GenerationIntegrityError):
        store.recover(client, active['generation_id'], active['contract']['embedding'],
                      expected_manifest_fingerprint=g.fingerprint(active))
    if location == 'pointer':
        assert external.read_text() == '{broken'


def test_notes_model_change_during_generation_blocks_publication(notes_builder):
    builder, client, store = notes_builder
    old, _ = builder.build_notes_generation()
    contract = {'provider': 'fake', 'model': 'fixed', 'revision': 'A', 'dimensions': 2}
    calls = 0
    def embed(text):
        nonlocal calls
        calls += 1
        if calls == 2:
            contract['revision'] = 'B'
        return [1, .5]
    with pytest.raises(ValueError, match='contract changed'):
        builder.build_notes_generation(rebuild=True, embed_fn=embed,
                                       embedding_contract_fn=lambda: dict(contract))
    assert calls == 2
    assert store.load_active() == old
    assert store.latest_attempt()['state'] == 'failed'


def test_pdf_writer_embeds_only_current_batch_before_first_write(pdf_builder, monkeypatch):
    builder, client, store = pdf_builder
    source = SimpleNamespace(file_id='Main', status='success')
    plan = SimpleNamespace(inventory=[source], documents=[],
        chunks=[SimpleNamespace(chunk_id=str(i), file_id='Main', text=str(i)) for i in range(205)])
    generation = candidate(store)
    embedded, at_write = [], []
    original = client.create_collection
    def create(**kwargs):
        col = original(**kwargs)
        add = col.add
        def observed_add(**values):
            at_write.append(len(embedded))
            add(**values)
        col.add = observed_add
        return col
    monkeypatch.setattr(client, 'create_collection', create)
    builder._write_candidate(client=client, store=store, generation=generation, plan=plan,
        embed_index_text=lambda text: embedded.append(text) or [1, .5],
        atomic_write_text=sys.modules['index_generation'].atomic_write_text)
    assert at_write == [100, 200, 205]
    assert len(embedded) == 205
