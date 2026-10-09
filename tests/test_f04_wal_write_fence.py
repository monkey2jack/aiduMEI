"""Cross-worker deletion/write integration; real SQLite, WAL and journal."""
import sqlite3

import pytest

from ducky import mutation_journal as journal
from ducky import wal_engine as we
pytest_plugins = ["test_f04_recovery"]


def job():
    return {'user_id': 'alice', 'bank_id': 'work', 'messages': 'synthetic queued body',
            'metadata': {}, 'infer': False}


@pytest.mark.parametrize('fault', ['delete_all', 'corrupt'])
@pytest.mark.parametrize('entry', ['accept_job', 'queued_execution', 'request', 'sdk'])
def test_wal_debt_blocks_every_write_before_journal_or_side_effect(world, fault, entry, tmp_path):
    mem, wal = world
    jid = journal.accept_job(job())
    if fault == 'delete_all':
        mem.client.points.add('original-point')
        mem.client.broken = True
        result = we.cascade_delete_all('alice', True, 'work')
        assert result['status'] == 'failed'
        original, = wal.get_pending_entries()
        expected = ('wal:' + original.wal_id, 'wal_delete_repair_required')
    else:
        wal.wal_file.write_text('{"format":2,"broken":')
        expected = ('wal:integrity', 'wal_integrity_unknown')
    with journal._db() as conn:
        before = [dict(row) for row in conn.execute('SELECT * FROM mutations')]
    effects = tmp_path / 'effects.db'
    with sqlite3.connect(effects) as conn:
        conn.execute('CREATE TABLE effects(body TEXT)')
    def callback():
        with sqlite3.connect(effects) as conn:
            conn.execute("INSERT INTO effects VALUES ('MUST NOT WRITE')")
        return {'status': 'ok'}
    with pytest.raises(journal.MutationUncertain) as error:
        if entry == 'accept_job':
            journal.accept_job(job())
        elif entry == 'queued_execution':
            with journal.execution_guard('alice', 'work', job_ids=[jid]):
                callback()
        elif entry == 'request':
            journal.execute_request('alice', 'work', {'messages': 'new request'}, callback)
        else:
            journal.perform_mutation('mem0_add', 'alice', 'work', {'messages': 'new SDK body'}, callback)
    assert (error.value.mutation_id, error.value.reason) == expected
    assert error.value.repair_source == 'wal'
    assert error.value.wal_id == (original.wal_id if fault == 'delete_all' else None)
    with journal._db() as conn:
        assert [dict(row) for row in conn.execute('SELECT * FROM mutations')] == before
    with sqlite3.connect(effects) as conn:
        assert conn.execute('SELECT COUNT(*) FROM effects').fetchone()[0] == 0


def test_failed_delete_can_erase_and_repair_then_new_writes_resume(world):
    mem, wal = world
    old_job = journal.accept_job(job())
    mem.client.points.add('original-point')
    mem.client.broken = True
    assert we.cascade_delete_all('alice', True, 'work')['status'] == 'failed'
    with pytest.raises(journal.MutationUncertain):
        journal.accept_job(job())
    # Write-only fence must not be applied to forgetting/repair entry points.
    with journal.forgetting_guard('alice', 'work'):
        journal.erase_scope('alice', 'work')
    mem.client.broken = False
    assert we.reconcile_startup()['remaining'] == 0
    assert not wal.get_pending_entries() and not mem.client.points
    with pytest.raises(journal.MutationUncertain):
        with journal.execution_guard('alice', 'work', job_ids=[old_job]):
            pytest.fail('forgotten old job ran after repair')
    fresh = journal.accept_job(job())
    with journal.execution_guard('alice', 'work', job_ids=[fresh]):
        result = journal.execute_request('alice', 'work', {}, lambda: {'status': 'ok'})
    assert result['status'] == 'ok'


def test_corrupt_wal_does_not_block_privacy_erasure(world):
    _, wal = world
    jid = journal.accept_job(job())
    wal.wal_file.write_bytes(b'unknown-current-ledger')
    with journal.forgetting_guard('alice', 'work'):
        assert journal.erase_scope('alice', 'work') == 1
    assert journal.job_record(jid)['status'] == 'forgotten'
    assert wal.wal_file.read_bytes() == b'unknown-current-ledger'


def test_deletion_fence_is_scope_specific(world):
    _, wal = world
    wal.append(we.WALEntry(user_id='alice', bank_id='work', operation='delete_all'))
    for user, bank in [('bob', 'work'), ('alice', 'other')]:
        assert journal.perform_mutation('mem0_add', user, bank, {}, lambda: {'results': []}) == {'results': []}


@pytest.mark.parametrize('fault', ['delete_all', 'corrupt'])
@pytest.mark.parametrize('method', ['add', 'update'])
def test_real_offline_sdk_cannot_write_through_wal_fence(world, tmp_path, monkeypatch, fault, method):
    import os
    import socket
    import ducky.mem0_runtime as rt
    from f04_durability_probe import offline_memory
    # Register restoration before the reusable offline factory installs its
    # network prohibition. Only external embedding is replaced, SDK is real.
    monkeypatch.setattr(socket.socket, 'connect', socket.socket.connect)
    monkeypatch.setattr(socket, 'getaddrinfo', socket.getaddrinfo)
    monkeypatch.setenv('MEM0_DIR', str(tmp_path / 'sdk-state'))
    monkeypatch.setenv('MEM0_TELEMETRY', 'false')
    real_makedirs = os.makedirs
    def private_makedirs(path, mode=0o777, exist_ok=False):
        if str(path) == '/tmp/qdrant':
            path = tmp_path / 'unused-sdk-default'
        return real_makedirs(path, mode=mode, exist_ok=exist_ok)
    monkeypatch.setattr(os, 'makedirs', private_makedirs)
    sdk = offline_memory(tmp_path / 'sdk')
    try:
        result = sdk.add('synthetic original body', user_id='alice', metadata={'bank_id': 'work'}, infer=False)
        mid = result['results'][0]['id']
        monkeypatch.setattr(rt, 'get_memory', lambda: sdk)
        _, wal = world
        if fault == 'delete_all':
            def fail_delete(*a, **kw):
                raise OSError('synthetic vector storage DELETE failed')
            monkeypatch.setattr(sdk.vector_store, 'delete', fail_delete)
            assert we.cascade_delete_all('alice', True, 'work')['status'] == 'failed'
        else:
            wal.wal_file.write_bytes(b'current corrupted WAL')
        before = sdk.get_all(filters={'user_id': 'alice'})
        with pytest.raises(journal.MutationUncertain) as error:
            if method == 'add':
                sdk.add('must never be added', user_id='alice', metadata={'bank_id': 'work'}, infer=False)
            else:
                sdk.update(mid, 'must never overwrite')
        assert error.value.reason.startswith('wal_')
        assert sdk.get_all(filters={'user_id': 'alice'}) == before
        assert sdk.get(mid)['memory'] == 'synthetic original body'
    finally:
        sdk.vector_store.client.close()
        telemetry = getattr(sdk, '_telemetry_vector_store', None)
        if telemetry is not None:
            telemetry.client.close()
