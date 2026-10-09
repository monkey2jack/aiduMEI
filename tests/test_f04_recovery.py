"""Real SQLite + product cascade/snapshot; fault injection only at storage I/O."""
import json
import sqlite3
import uuid
from types import SimpleNamespace

import pytest

import ducky.wal_engine as we
import ducky.tombstone as ts
import ducky.dual_index as di
import ducky.utils as utils


class LocalStore:
    def __init__(self):
        self.points = set()
        self.broken = False

    def get_collections(self):
        return SimpleNamespace(collections=[SimpleNamespace(name='mem0_local')])

    def retrieve(self, collection_name, ids, **kw):
        # Qdrant 的 HTTP 协议不接受结构化事实键作为点位 ID。
        for point_id in ids:
            if type(point_id) is not int:
                uuid.UUID(point_id)
        return [SimpleNamespace(id=i, payload={'user_id': 'alice', 'bank_id': 'work'}) for i in ids if i in self.points]

    def count(self, *a, **kw):
        return SimpleNamespace(count=len(self.points))

    def delete(self, collection_name, points_selector, **kw):
        if self.broken:
            raise OSError('injected local DELETE failure')
        if isinstance(points_selector, list):
            self.points.difference_update(points_selector)
        else:
            self.points.clear()


class Memory:
    def __init__(self):
        self.items = {}
        self.vector_store = self
        self.client = LocalStore()
        self.broken = False
        self.embedding_model = SimpleNamespace(embed=lambda text, memory_action=None: [.1, .2, .3])

    def get_all(self, **kw):
        return {'results': list(self.items.values())}

    def get(self, vector_id):
        r = self.items.get(vector_id)
        return {'payload': {'data': r['memory'], 'user_id': 'alice', 'bank_id': 'work', **r.get('metadata', {})}} if r else None

    def insert(self, vectors, payloads=None, ids=None):
        for mid, payload in zip(ids, payloads):
            self.items[mid] = {'id': mid, 'memory': payload['data'], 'user_id': payload['user_id'], 'metadata': {k: v for k, v in payload.items() if k != 'data'}}

    def delete(self, mid):
        if self.broken:
            raise OSError('injected vector DELETE failure')
        self.items.pop(mid, None)


@pytest.fixture
def world(tmp_path, monkeypatch):
    import ducky.mem0_runtime as rt
    import ducky.memory_types as mt
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.text_fts import _init_text_fts
    monkeypatch.setattr(utils, 'FACTS_DB', str(tmp_path / 'facts.db'))
    monkeypatch.setattr(utils, 'TEXT_FTS_DB', str(tmp_path / 'fts.db'))
    monkeypatch.setattr(mt, '_checked', False)
    monkeypatch.setenv('AIDUMEI_ENGINE_MODE', 'cloud')
    ensure_core_schema(force=True)
    _init_text_fts()
    ts.ensure_tombstone_schema()
    mem = Memory()
    monkeypatch.setattr(rt, 'get_memory', lambda: mem)
    monkeypatch.setattr(di, '_qdrant_client', lambda: mem.client)
    monkeypatch.setattr(di, 'spawn_replay_daemon', lambda **kw: False)
    wal = we.WALEngine(str(tmp_path / 'wal'))
    monkeypatch.setattr(we.WALEngine, '_instance', wal)
    return mem, wal


def fact(mid, category, value, owner='alice', source='alice'):
    c = utils.get_facts_conn()
    c.execute('INSERT INTO facts(category,fact_key,fact_value,user_id,bank_id,agent_id,source) VALUES(?,?,?,?,?,?,?)',
              (category, mid, value, owner, 'work', source, source))
    c.commit()
    c.close()


def test_multiline_snapshot_and_restore_exact_set(world):
    _, wal = world
    mid = 'structured-multi'
    fact(mid, 'a', 'unique first')
    fact(mid, 'b', 'unique second')
    fact(mid, 'c', 'foreign must not snapshot', owner='bob')
    out = we.cascade_delete_memory(mid, user_id='alice', bank_id='work')
    assert out['status'] == 'committed', out
    tid = out['details']['tombstone_id']
    result = ts.restore_tombstone(tid, user_id='alice', bank_id='work')
    assert result['restored'], result
    c = utils.get_facts_conn()
    assert {(r['category'], r['fact_value']) for r in c.execute("SELECT * FROM facts WHERE user_id='alice'")} == {('a', 'unique first'), ('b', 'unique second')}
    assert c.execute("SELECT COUNT(*) FROM facts WHERE user_id='bob'").fetchone()[0] == 1
    assert wal.get_pending_entries() == []


def test_partial_snapshot_read_blocks_auto_merge(world, monkeypatch):
    mem, _ = world
    import ducky.layer1_selfcheck as layer
    monkeypatch.setenv('AIDUMEI_AUTO_MERGE', 'on')
    for i in (1, 2, 3):
        mid = str(uuid.UUID(int=i))
        mem.items[mid] = {'id': mid, 'memory': 'same synthetic memory', 'user_id': 'alice', 'metadata': {'bank_id': 'work'}, 'created_at': str(i)}
        fact(mid, 'general', 'unique structured detail ' + str(i))
    real = ts.get_facts_conn

    class ReadFault:
        def __getattr__(self, attr):
            return getattr(real(), attr)

        def execute(self, sql, *args, **kw):
            if sql.startswith('SELECT * FROM facts WHERE'):
                raise sqlite3.OperationalError('injected facts SELECT failure')
            return real().execute(sql, *args, **kw)

    monkeypatch.setattr(ts, 'get_facts_conn', ReadFault)
    assert layer.auto_merge_similar(mem, 'alice', bank_id='work')['deleted'] == 0
    assert len(mem.items) == 3
    assert real().execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 3


@pytest.mark.parametrize('all_scope', [False, True])
def test_local_failure_is_retryable_on_original_wal(world, all_scope):
    mem, wal = world
    mid = str(uuid.UUID(int=17))
    mem.client.points.add(mid)
    mem.client.broken = True
    out = (we.cascade_delete_all('alice', confirm=True, bank_id='work') if all_scope
           else we.cascade_delete_memory(mid, user_id='alice', bank_id='work'))
    assert out['status'] in ('failed', 'partial'), out
    assert any('local' in f['layer'] for f in out['failed_layers'])
    pending = wal.get_pending_entries()
    assert len(pending) == 1
    mem.client.broken = False
    report = we.reconcile_startup()
    assert report['recovered'] == 1 and report['failed'] == 0, report
    assert not mem.client.points
    assert wal.get_pending_entries() == []


def test_startup_failure_stays_retryable_not_recovered(world):
    mem, wal = world
    mid = str(uuid.UUID(int=1))
    mem.items[mid] = {'id': mid, 'memory': 'synthetic', 'user_id': 'alice', 'metadata': {'bank_id': 'work'}}
    mem.broken = True
    wal.append(we.WALEntry(wal_id='original', operation='delete', user_id='alice', bank_id='work', payload={'memory_id': mid, 'bank_id': 'work'}))
    report = we.reconcile_startup()
    assert report['recovered'] == 0 and report['failed'] == 1
    assert [e.wal_id for e in wal.get_pending_entries()] == ['original']
    mem.broken = False
    assert we.reconcile_startup()['recovered'] == 1
    assert not mem.items


@pytest.mark.parametrize('change', [{'status': 'pendnig'}, {'payload': []}, {'timestamp': 'bad'}, {'timestamp': 10 ** 400}, {'operation': 'anything'}, {'payload': {'target_wal_id': 'x'}}])
def test_schema_invalid_preserves_original(tmp_path, change):
    w = we.WALEngine(str(tmp_path))
    row = json.loads(we.WALEntry(wal_id='x', operation='delete', payload={'memory_id': 'm'}).to_json())
    row.update(change)
    w.wal_file.write_text(json.dumps(row) + '\n')
    before = w.wal_file.read_bytes()
    with pytest.raises(we.WALIntegrityError):
        w.get_pending_entries()
    try:
        w.compact(0)
    except we.WALIntegrityError:
        pass
    assert w.wal_file.read_bytes() == before


def test_duplicate_intent_id_rejected_without_discard(tmp_path):
    w = we.WALEngine(str(tmp_path))
    w.wal_file.write_text(''.join(we.WALEntry(wal_id='duplicate', operation='delete', payload={'memory_id': m}).to_json() + '\n' for m in ('one', 'two')))
    before = w.wal_file.read_bytes()
    with pytest.raises(we.WALIntegrityError):
        w.get_pending_entries()
    try:
        w.compact(0)
    except we.WALIntegrityError:
        pass
    assert w.wal_file.read_bytes() == before


def test_valid_json_tamper_and_reorder_detected(tmp_path):
    w = we.WALEngine(str(tmp_path))
    for mid in ('one', 'two'):
        w.append(we.WALEntry(wal_id=mid, operation='delete', payload={'memory_id': mid}))
    original = w.wal_file.read_text()
    w.wal_file.write_text(original.replace('"one"', '"changed"'))
    with pytest.raises(we.WALIntegrityError):
        w.get_pending_entries()
    w.wal_file.write_text('\n'.join(reversed(original.splitlines())) + '\n')
    with pytest.raises(we.WALIntegrityError):
        w.get_pending_entries()


def test_legacy_failed_intent_survives_compaction_and_is_visible(tmp_path):
    w = we.WALEngine(str(tmp_path))
    w.wal_file.write_text(we.WALEntry(wal_id='old-failed', timestamp=1, operation='delete', payload={'memory_id': 'm'}, status='failed', error='backend unavailable').to_json() + '\n')
    assert w.compact(0)['kept'] == 1
    assert [e.wal_id for e in w.get_pending_entries()] == ['old-failed']


def test_legacy_wal_upgrade_preserves_original_and_status(tmp_path):
    w = we.WALEngine(str(tmp_path))
    initial = we.WALEntry(wal_id='old', operation='delete', payload={'memory_id': 'old-memory'})
    raw = initial.to_json() + '\n'
    w.wal_file.write_text(raw)
    w.append(we.WALEntry(wal_id='new', operation='delete', payload={'memory_id': 'new-memory'}))
    mixed = w.wal_file.read_bytes()
    w.compact(0)
    assert w.wal_file.with_name(w.wal_file.name + '.pre-f04').read_bytes() == mixed
    assert {e.wal_id for e in w.get_pending_entries()} == {'old', 'new'}
    w.mark_status('old', 'committed')
    assert [e.wal_id for e in w.get_pending_entries()] == ['new']


def test_legacy_dict_tombstone_still_restores(world):
    c = utils.get_facts_conn()
    cur = c.execute("INSERT INTO tombstones(target_id,user_id,bank_id,facts_snapshot) VALUES(?,?,?,?)",
                    ('legacy-id', 'alice', 'work', json.dumps({'category': 'old', 'fact_key': 'legacy-id', 'fact_value': 'legacy complete body', 'user_id': 'alice', 'bank_id': 'work'})))
    tid = cur.lastrowid
    c.commit()
    result = ts.restore_tombstone(tid, 'alice', 'work')
    assert result['restored'], result
    assert c.execute("SELECT fact_value FROM facts WHERE fact_key='legacy-id'").fetchone()[0] == 'legacy complete body'


def test_changed_facts_after_snapshot_are_preserved(world, monkeypatch):
    fact('fact-race', 'first', 'original')
    real_snapshot = ts.snapshot_before_delete
    def snapshot_then_concurrent_write(*args, **kw):
        tid = real_snapshot(*args, **kw)
        fact('fact-race', 'second', 'arrived after snapshot')
        return tid
    monkeypatch.setattr(ts, 'snapshot_before_delete', snapshot_then_concurrent_write)
    result = we.cascade_delete_memory('fact-race', 'alice', 'work')
    assert result['status'] == 'failed'
    assert utils.get_facts_conn().execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 2
    assert world[1].get_pending_entries()


def test_raw_handle_snapshot_matches_actual_delete(world):
    digest = 'a' * 32
    mid = 'raw-' + digest + '-deadbeef'
    fact('raw:' + digest, 'a', 'raw one')
    fact('raw:' + digest, 'b', 'raw two')
    out = we.cascade_delete_memory(mid, 'alice', 'work')
    assert out['status'] == 'committed', out
    assert out['details']['facts'] == 2
    tid = out['details']['tombstone_id']
    assert ts.restore_tombstone(tid, 'alice', 'work')['restored']
    assert utils.get_facts_conn().execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 2


@pytest.mark.parametrize('scope_delete', [False, True])
def test_journal_sqlite_erase_failure_retains_intent_before_vectors(world, monkeypatch, scope_delete):
    import ducky.mutation_journal as journal
    from contextlib import contextmanager
    real_db = journal._db
    @contextmanager
    def broken_db():
        with real_db() as conn:
            class Fault:
                def __getattr__(self, name):
                    return getattr(conn, name)
                def execute(self, sql, *args):
                    if 'privacy_erased' in sql:
                        raise sqlite3.OperationalError('injected journal secure erase failure')
                    return conn.execute(sql, *args)
            yield Fault()
    mem, wal = world
    mid = str(uuid.UUID(int=1))
    mem.items[mid] = {'id': mid, 'memory': 'sensitive synthetic text', 'user_id': 'alice', 'metadata': {'bank_id': 'work'}}
    journal.accept_job({'user_id': 'alice', 'bank_id': 'work', 'messages': 'synthetic pending input', 'metadata': {}, 'infer': False})
    monkeypatch.setattr(journal, '_db', broken_db)
    out = (we.cascade_delete_all('alice', True, 'work') if scope_delete else we.cascade_delete_memory(mid, 'alice', 'work'))
    assert out['status'] == 'failed'
    assert mid in mem.items
    assert out['failed_layers'][0]['layer'] == 'mutation_journal'
    assert len(wal.get_pending_entries()) == 1


def test_partial_restore_of_two_rows_is_atomic_and_retryable(world):
    fact('restore-two', 'a', 'one')
    fact('restore-two', 'b', 'two')
    out = we.cascade_delete_memory('restore-two', 'alice', 'work')
    tid = out['details']['tombstone_id']
    c = utils.get_facts_conn()
    # Real SQLite trigger rejects the SECOND row; the first must roll back.
    c.execute("CREATE TRIGGER reject_second BEFORE INSERT ON facts WHEN new.category='b' BEGIN SELECT RAISE(ABORT,'injected row failure'); END")
    c.commit()
    result = ts.restore_tombstone(tid, 'alice', 'work')
    assert result['status'] == 'partial'
    assert c.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 0
    assert c.execute('SELECT restored_at FROM tombstones WHERE tombstone_id=?', (tid,)).fetchone()[0] is None
    c.execute('DROP TRIGGER reject_second'); c.commit()
    assert ts.restore_tombstone(tid, 'alice', 'work')['restored']
    assert c.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 2


def test_two_fts_aliases_restore_their_original_content(world):
    from ducky.bank_contract import make_scope, scoped_storage_key
    from ducky.text_fts import _index_memory
    mid = 'fts-two'
    _index_memory(mid, 'first alias content', user_id='alice', bank_id='work')
    tc = utils.get_text_conn()
    sid = scoped_storage_key(mid, make_scope('alice', 'work'))
    row = dict(tc.execute('SELECT * FROM memories WHERE id=?', (sid,)).fetchone())
    row.update(id='fact:' + sid, content='second distinct alias')
    cols = list(row)
    tc.execute(f"INSERT INTO memories ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})", tuple(row[k] for k in cols)); tc.commit()
    out = we.cascade_delete_memory(mid, 'alice', 'work')
    assert out['status'] == 'committed', out
    assert out['details']['fts_rows'] == 2
    assert ts.restore_tombstone(out['details']['tombstone_id'], 'alice', 'work')['restored']
    assert {r['content'] for r in tc.execute('SELECT content FROM memories')} == {'first alias content', 'second distinct alias'}


def test_pending_delete_blocks_writes_until_repaired(world):
    _, wal = world
    wal.append(we.WALEntry(wal_id='erase-in-progress', user_id='alice', bank_id='work', operation='delete_all', payload={'bank_id': 'work'}))
    with pytest.raises(we.DeletionPending):
        we.assert_scope_writable('alice', 'work')
    we.assert_scope_writable('bob', 'work')
    we.assert_scope_writable('alice', 'home')
    wal.mark_status('erase-in-progress', 'committed')
    we.assert_scope_writable('alice', 'work')


@pytest.mark.parametrize('layer', ['fts', 'vector', 'verbatim'])
def test_each_snapshot_layer_unknown_blocks_delete(world, monkeypatch, layer):
    mem, wal = world
    mid = str(uuid.UUID(int=1))
    mem.items[mid] = {'id': mid, 'memory': 'preserved', 'user_id': 'alice', 'metadata': {'bank_id': 'work'}}
    fact(mid, 'source', 'preserved')
    if layer == 'vector':
        def broken_get(vector_id):
            raise OSError('injected backend GET failure')
        monkeypatch.setattr(mem, 'get', broken_get)
    else:
        method = 'get_text_conn' if layer == 'fts' else 'get_facts_conn'
        original = getattr(ts, method)
        class ReadFault:
            def __init__(self):
                self.c = original()
            def __getattr__(self, name):
                return getattr(self.c, name)
            def execute(self, sql, *args):
                table = 'memories' if layer == 'fts' else 'verbatim_turns'
                if sql.startswith('SELECT * FROM ' + table):
                    raise sqlite3.OperationalError('injected layer SELECT failure')
                return self.c.execute(sql, *args)
        if layer == 'verbatim':
            from ducky.verbatim_vault import ensure_verbatim_schema
            ensure_verbatim_schema()
        monkeypatch.setattr(ts, method, ReadFault)
    result = we.cascade_delete_memory(mid, 'alice', 'work')
    assert result['status'] == 'failed'
    assert mid in mem.items
    assert utils.get_facts_conn().execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 1
    assert wal.get_pending_entries()


def test_verbatim_multisession_rows_restore_with_original_ids(world):
    from ducky.verbatim_vault import store_verbatim, ensure_verbatim_schema
    ensure_verbatim_schema()
    text = 'Synthetic text across two independent sessions'
    for session in ('s1', 's2'):
        store_verbatim('alice', text, {'session_id': session}, bank_id='work')
    from ducky.text_fts import _index_memory
    _index_memory('source-record', text, user_id='alice', bank_id='work')
    c = utils.get_facts_conn()
    before = [dict(row) for row in c.execute('SELECT * FROM verbatim_turns ORDER BY id')]
    assert len(before) == 2
    out = we.cascade_delete_memory('source-record', 'alice', 'work')
    assert out['status'] == 'committed', out
    assert c.execute('SELECT COUNT(*) FROM verbatim_turns').fetchone()[0] == 0
    restored = ts.restore_tombstone(out['details']['tombstone_id'], 'alice', 'work')
    assert restored['restored'], restored
    assert [dict(row) for row in c.execute('SELECT * FROM verbatim_turns ORDER BY id')] == before
    assert utils.get_text_conn().execute('SELECT COUNT(*) FROM verbatim_fts_map').fetchone()[0] == 2


def test_local_copy_ownership_is_checked(world, monkeypatch):
    mem, _ = world
    mid = str(uuid.UUID(int=18))
    mem.client.points.add(mid)
    monkeypatch.setattr(mem.client, 'retrieve', lambda **kw: [SimpleNamespace(id=mid, payload={'user_id': 'bob', 'bank_id': 'work'})])
    out = we.cascade_delete_memory(mid, 'alice', 'work')
    assert out['status'] == 'not_found', out
    assert mem.client.points == {mid}


def test_local_copy_unknown_owner_is_repairable_failure(world, monkeypatch):
    mem, wal = world
    mid = str(uuid.UUID(int=19))
    mem.client.points.add(mid)
    monkeypatch.setattr(mem.client, 'retrieve', lambda **kw: [SimpleNamespace(id=mid, payload={})])
    result = we.cascade_delete_memory(mid, 'alice', 'work')
    assert result['status'] == 'failed'
    assert mem.client.points == {mid}
    assert wal.get_pending_entries()


def test_verbatim_local_failure_repairs_after_primary_row_is_gone(world):
    from ducky.verbatim_vault import store_verbatim
    text = 'Synthetic verbatim repair must retain this exact local key'
    store_verbatim('alice', text, {'session_id': 'repair'}, bank_id='work')
    turn_id = utils.get_facts_conn().execute('SELECT id FROM verbatim_turns').fetchone()[0]
    mem, wal = world
    pid = di.verbatim_local_pid('alice', 'work', text)
    mem.client.points.add(pid)
    mem.client.broken = True
    out = we.cascade_delete_memory('verbatim:' + str(turn_id), 'alice', 'work')
    assert out['status'] == 'failed'
    assert utils.get_facts_conn().execute('SELECT COUNT(*) FROM verbatim_turns').fetchone()[0] == 0
    assert pid in mem.client.points
    original_id = wal.get_pending_entries()[0].wal_id
    mem.client.broken = False
    report = we.reconcile_startup()
    assert report['recovered'] == 1, report
    assert not mem.client.points
    assert not wal.get_pending_entries()
    assert next(e for e in wal.entries() if e.wal_id == original_id).status == 'committed'


def test_raw_handle_restores_complete_vector_set(world):
    mem, _ = world
    digest = 'b' * 32
    ids = [str(uuid.UUID(int=i)) for i in (4, 5)]
    for i, mid in enumerate(ids):
        mem.items[mid] = {'id': mid, 'memory': 'raw vector unique ' + str(i), 'user_id': 'alice', 'metadata': {'bank_id': 'work', 'content_hash': digest}}
    original = {mid: r['memory'] for mid, r in mem.items.items()}
    out = we.cascade_delete_memory('raw-' + digest + '-01234567', 'alice', 'work')
    assert out['status'] == 'committed', out
    assert not mem.items
    restored = ts.restore_tombstone(out['details']['tombstone_id'], 'alice', 'work')
    assert restored['restored'], restored
    assert {mid: r['memory'] for mid, r in mem.items.items()} == original


def test_legacy_unbounded_scope_delete_never_erases_newer_data(world):
    mem, wal = world
    mem.items['newer'] = {'id': 'newer', 'user_id': 'alice', 'metadata': {'bank_id': 'work'}}
    raw = we.WALEntry(wal_id='old-scope', timestamp=1, user_id='alice', operation='delete_all', payload={'bank_id': 'work'}, status='failed').to_json() + '\n'
    wal.wal_file.write_text(raw)
    report = we.reconcile_startup()
    assert report['recovered'] == 0 and report['review_required'] == ['old-scope']
    assert 'newer' in mem.items
    assert wal.wal_file.with_name(wal.wal_file.name + '.pre-f04').read_text() == raw
    assert wal.recovery_status()['needs_attention']


def test_corrupt_startup_and_health_report_unknown_counts(world, monkeypatch):
    _, wal = world
    wal.wal_file.write_text('{"wal_id":\n')
    spawned = []
    monkeypatch.setattr(di, 'spawn_replay_daemon', lambda **kw: spawned.append(kw))
    report = we.reconcile_startup()
    assert report['reconciliation_paused'] is True
    assert report['pending_count'] is None and report['remaining'] is None
    assert report['review_required'] == []
    assert report['reconciled_at'] > 0
    health = wal.recovery_status()
    assert health['needs_attention'] is True and health['integrity'] == 'unknown'
    assert health['pending'] is None and health['failed'] is None
    assert health['operations'] == []
    assert spawned == []


def test_unresolved_deletion_blocks_startup_embedding_replay(world, monkeypatch):
    mem, wal = world
    mid = str(uuid.UUID(int=1))
    mem.items[mid] = {'id': mid, 'memory': 'preserved', 'user_id': 'alice', 'metadata': {'bank_id': 'work'}}
    mem.broken = True
    wal.append(we.WALEntry(wal_id='repair-first', operation='delete', user_id='alice', bank_id='work', payload={'memory_id': mid}))
    spawned = []
    monkeypatch.setattr(di, 'spawn_replay_daemon', lambda **kw: spawned.append(kw) or True)
    report = we.reconcile_startup()
    assert report['remaining'] == 1 and report['recovered'] == 0
    assert report['pending_replay_spawned'] is False and spawned == []
    mem.broken = False
    report = we.reconcile_startup()
    assert report['remaining'] == 0 and report['recovered'] == 1
    assert report['pending_replay_spawned'] is True and len(spawned) == 1


@pytest.mark.parametrize('point', [{}, {'error': 'unavailable'}, {'payload': []}])
def test_unknown_vector_payload_blocks_snapshot_and_delete(world, monkeypatch, point):
    mem, wal = world
    mid = str(uuid.UUID(int=1))
    mem.items[mid] = {'id': mid, 'memory': 'vector body', 'user_id': 'alice', 'metadata': {'bank_id': 'work'}}
    fact(mid, 'primary', 'distinct structured body')
    monkeypatch.setattr(mem, 'get', lambda vector_id: point)
    result = we.cascade_delete_memory(mid, 'alice', 'work')
    assert result['status'] == 'failed'
    assert mid in mem.items
    assert utils.get_facts_conn().execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 1
    assert wal.get_pending_entries()


def test_raw_vector_payload_without_source_text_blocks_delete(world, monkeypatch):
    mem, wal = world
    mid = str(uuid.UUID(int=4))
    digest = 'e' * 32
    mem.items[mid] = {'id': mid, 'memory': 'preserve vector source', 'user_id': 'alice', 'metadata': {'bank_id': 'work', 'content_hash': digest}}
    fact('raw:' + digest, 'general', 'distinct fact source')
    monkeypatch.setattr(mem, 'get', lambda vector_id: {'payload': {'user_id': 'alice', 'bank_id': 'work', 'content_hash': digest}})
    result = we.cascade_delete_memory('raw-' + digest + '-01234567', 'alice', 'work')
    assert result['status'] == 'failed'
    assert mid in mem.items
    assert utils.get_facts_conn().execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 1
    assert wal.get_pending_entries()
