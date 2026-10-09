"""Real SQLite, real offline mem0/Qdrant and process-death durability tests."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading

import pytest

from ducky import mutation_journal as journal
from ducky import utils

PROBE = Path(__file__).with_name('f04_durability_probe.py')


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    (tmp_path / 'data').mkdir()
    monkeypatch.setattr(utils, 'DATA_DIR', str(tmp_path / 'data'))
    monkeypatch.setattr(utils, 'FACTS_DB', str(tmp_path / 'data' / 'facts.db'))
    monkeypatch.setenv('MEM0_TELEMETRY', 'false')
    return tmp_path


def rows():
    conn = sqlite3.connect(journal.journal_path())
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute('SELECT * FROM mutations ORDER BY created_at')]
    finally:
        conn.close()


def child(root, mode, code=0):
    env = {**os.environ, 'PYTHONPATH': str(PROBE.parent.parent), 'MEM0_TELEMETRY': 'false'}
    completed = subprocess.run([sys.executable, str(PROBE), str(root), mode],
                               env=env, capture_output=True, text=True, timeout=30)
    assert completed.returncode == code, completed.stderr + completed.stdout
    return completed


def test_intent_precedes_side_effect_and_ack_precedes_return(isolated):
    def write():
        row, = rows()
        assert row['state'] == 'started'
        assert json.loads(row['input_json']) == {'messages': 'synthetic'}
        with sqlite3.connect(isolated / 'effect.db') as conn:
            conn.execute('CREATE TABLE records(body TEXT)')
            conn.execute("INSERT INTO records VALUES ('synthetic')")
        return {'results': [{'id': 'memory-1'}]}
    assert journal.perform_mutation('mem0_add', 'u', 'b', {'messages': 'synthetic'}, write)['results']
    row, = rows()
    assert row['state'] == 'acknowledged'
    assert json.loads(row['result_json'])['results'][0]['id'] == 'memory-1'
    assert journal.journal_path().stat().st_mode & 0o777 == 0o600


def test_no_sdk_dispatch_when_intent_storage_fails(isolated, monkeypatch):
    called = []
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic disk full')
    monkeypatch.setattr(journal, '_insert', fail)
    with pytest.raises(sqlite3.OperationalError):
        journal.perform_mutation('mem0_add', 'u', 'b', {}, lambda: called.append(1))
    assert not called


def test_ack_failure_never_returns_success_or_redispatches(isolated, monkeypatch):
    called = []
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic disk full')
    monkeypatch.setattr(journal, '_ack', fail)
    with pytest.raises(journal.MutationUncertain):
        journal.perform_mutation('mem0_add', 'u', 'b', {}, lambda: called.append(1) or {'results': []})
    with pytest.raises(journal.MutationUncertain):
        journal.perform_mutation('mem0_add', 'u', 'b', {}, lambda: called.append(2))
    assert called == [1]
    assert rows()[0]['state'] == 'repair_required'
    assert journal.journal_health()['status'] == 'degraded'


@pytest.mark.parametrize('mode,exit_code', [('add_exit', 71), ('ack_exit', 72)])
def test_real_sdk_add_process_death_before_or_after_ack(isolated, mode, exit_code):
    child(isolated, mode, exit_code)
    child(isolated, 'inspect_sdk')
    actual = json.loads((isolated / 'sdk-records.json').read_text())
    assert len(actual) == 1 and actual[0]['memory'] == 'synthetic durable secret'
    recovered = json.loads((isolated / 'recovery.json').read_text())
    assert recovered['recovered'] == 0  # never blindly replayed the real SDK
    assert recovered['held_for_repair'] == (1 if mode == 'add_exit' else 0)
    assert rows()[0]['state'] == ('repair_required' if mode == 'add_exit' else 'acknowledged')
    if mode == 'add_exit':
        assert 'synthetic durable secret' in rows()[0]['input_json']
        with pytest.raises(journal.MutationUncertain):
            journal.perform_mutation('mem0_add', 'synthetic-owner', 'work', {}, lambda: pytest.fail('duplicate'))


def test_real_sdk_update_death_preserves_input_and_existing_target(isolated):
    child(isolated, 'ack_exit', 72)
    child(isolated, 'update_exit', 71)
    child(isolated, 'inspect_sdk')
    actual = json.loads((isolated / 'sdk-records.json').read_text())
    assert len(actual) == 1 and actual[0]['memory'] == 'synthetic updated secret'
    update = [row for row in rows() if row['kind'] == 'mem0_update'][0]
    assert update['state'] == 'repair_required'
    assert update['target_id'] == actual[0]['id']
    assert 'synthetic updated secret' in update['input_json']


@pytest.mark.parametrize('mode,exit_code', [('job_exit', 73), ('coalesce_exit', 74)])
def test_accepted_original_survives_process_death(isolated, mode, exit_code):
    child(isolated, mode, exit_code)
    child(isolated, 'recover')
    row, = rows()
    assert row['state'] == 'repair_required'
    payload = json.loads(row['input_json'])
    assert payload['messages'][0]['content'].endswith('complete synthetic text')
    assert payload['infer'] is False
    from ducky.speed.jobs import job_get
    result = job_get(row['id'], user_id='synthetic-owner', bank_id='work')
    assert result['status'] == 'repair_required' and result['durable'] is False
    assert job_get(row['id'], user_id='wrong-owner', bank_id='work') is None


def test_scope_and_target_privacy_erasure_cancels_stale_queue(isolated):
    from ducky.speed.jobs import job_create, job_get
    jid = job_create({'user_id': 'u', 'bank_id': 'b', 'messages': 'UNIQUE_ERASE_SECRET_190274',
                      'text_preview': 'UNIQUE_ERASE_SECRET_190274'})
    other = job_create({'user_id': 'u', 'bank_id': 'other', 'messages': 'keep'})
    with journal.forgetting_guard('u', 'b'):
        assert journal.erase_target('u', 'b', 'memory-x') == 1
    assert job_get(jid)['status'] == 'forgotten'
    assert job_get(jid)['payload_preview'] == ''
    with pytest.raises(journal.MutationUncertain):
        with journal.execution_guard('u', 'b', job_ids=[jid]):
            pytest.fail('stale queue resurrected')
    assert journal.inspect_mutation(other, 'u', 'other', include_payload=True)['input']['messages'] == 'keep'
    # Physical DB pages, not merely SQL SELECT, no longer carry the payload.
    assert b'UNIQUE_ERASE_SECRET_190274' not in journal.journal_path().read_bytes()
    assert not Path(str(journal.journal_path()) + '-wal').exists()


def test_resolve_requires_scope_evidence_and_never_replays(isolated):
    with pytest.raises(journal.MutationUncertain):
        journal.perform_mutation('mem0_add', 'u', 'b', {'text': 'source'},
                                 lambda: (_ for _ in ()).throw(OSError('synthetic')))
    mid = rows()[0]['id']
    with pytest.raises(KeyError):
        journal.resolve_mutation(mid, 'other', 'b', resolution='confirmed_applied', evidence='checked')
    with pytest.raises(ValueError):
        journal.resolve_mutation(mid, 'u', 'b', resolution='confirmed_applied', evidence='')
    out = journal.resolve_mutation(mid, 'u', 'b', resolution='confirmed_not_applied', evidence='backend verified absent')
    assert out['state'] == 'not_applied' and out['automatic_replay'] is False
    assert journal.journal_health()['status'] == 'ok'


def test_keyed_request_ack_survives_missing_old_receipt(isolated):
    from ducky import idempotency
    payload = {'messages': 'synthetic', 'bank_id': 'b'}
    count = []
    result = journal.execute_request('u', 'b', payload,
              lambda: count.append(1) or {'status': 'ok', 'id': 'synthetic-id'}, request_key='stable')
    assert result['status'] == 'ok'
    replay = idempotency.claim('stable', 'u', 'b', payload)
    assert replay['action'] == 'replay' and replay['response']['id'] == 'synthetic-id'
    assert idempotency.claim('stable', 'u', 'b', {'messages': 'different'})['action'] == 'conflict'
    journal.execute_request('u', 'b', payload, lambda: pytest.fail('duplicate'), request_key='stable')
    assert count == [1]


def test_persistent_job_does_not_expire_into_duplicate_claim(isolated, monkeypatch):
    from ducky import idempotency
    from ducky.speed.jobs import job_create, job_update
    payload = {'messages': 'synthetic'}
    state = idempotency.claim('stable', 'u', 'b', payload)
    jid = job_create({'user_id': 'u', 'bank_id': 'b', **payload,
                     'idempotency': idempotency.job_binding(state, 'stable', 'u', 'b')})
    idempotency.finalize('stable', 'u', 'b', {'status': 'accepted', 'durable': False}, provisional=True)
    monkeypatch.setattr(idempotency.time, 'time', lambda: 9_000_000_000)
    assert idempotency.claim('stable', 'u', 'b', payload)['action'] == 'replay'
    job_update(jid, status='running')
    job_update(jid, status='error', error='uncertain')
    held = idempotency.claim('stable', 'u', 'b', payload)
    assert held['action'] == 'pending' and held['reason'] == 'repair_required'


def test_corruption_and_unknown_schema_fail_closed(isolated):
    journal.journal_path().write_bytes(b'not a database')
    assert journal.journal_health()['status'] == 'degraded'
    with pytest.raises(sqlite3.DatabaseError):
        journal.perform_mutation('mem0_add', 'u', 'b', {}, lambda: pytest.fail('unsafe'))


def test_recovery_waits_for_live_writer_and_rechecks_ack(isolated):
    entered, release = threading.Event(), threading.Event()
    result = {}
    def write():
        entered.set()
        assert release.wait(5)
        return {'results': []}
    t = threading.Thread(target=lambda: journal.perform_mutation('mem0_add', 'u', 'b', {}, write))
    t.start()
    assert entered.wait(5)
    def recover():
        result.update(journal.startup_recover())
    r = threading.Thread(target=recover)
    r.start()
    release.set()
    t.join(5); r.join(5)
    assert not t.is_alive() and not r.is_alive()
    assert result['held_for_repair'] == 0 and rows()[0]['state'] == 'acknowledged'


def test_forgetting_clears_coalesce_preview_as_well_as_database(isolated):
    from ducky.speed import coalesce
    coalesce.coalesce_enqueue('u', 'synthetic forgotten body', {'no_fastpath': True}, bank_id='b')
    assert coalesce.coalesce_status('u')['buffer_count'] == 1
    with journal.forgetting_guard('u', 'b'):
        journal.erase_scope('u', 'b')
    assert coalesce.coalesce_status('u')['buffer_count'] == 0


def test_coalesce_cannot_attach_input_to_another_scope_job(isolated):
    from ducky.speed import coalesce
    jid = journal.accept_job({'user_id': 'u', 'bank_id': 'private', 'messages': 'keep'})
    with pytest.raises(ValueError, match='scope mismatch'):
        coalesce.coalesce_enqueue('u', 'wrong scope', job_id=jid, bank_id='public')
    assert journal.inspect_mutation(jid, 'u', 'private', include_payload=True)['input']['messages'] == 'keep'


def test_ack_also_failing_to_mark_uncertain_still_blocks_retry(isolated, monkeypatch):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError('synthetic disk unavailable')
    monkeypatch.setattr(journal, '_ack', fail)
    monkeypatch.setattr(journal, '_uncertain', fail)
    with pytest.raises(journal.MutationUncertain):
        journal.perform_mutation('mem0_add', 'u', 'b', {}, lambda: {'results': []})
    assert rows()[0]['state'] == 'started'
    with pytest.raises(journal.MutationUncertain):
        journal.perform_mutation('mem0_add', 'u', 'b', {}, lambda: pytest.fail('duplicate'))
    journal.startup_recover()
    assert rows()[0]['state'] == 'repair_required'


def test_process_flock_serializes_recovery_with_live_sdk(isolated):
    # Parent and child use independent threading locks; only POSIX flock can
    # serialize them. Explicit ready marker removes scheduler timing guesses.
    marker = isolated / 'child-at-recovery'
    output = isolated / 'child-recovery-result'
    source = '''
import json, os, pathlib
from ducky import utils
utils.FACTS_DB = os.environ['SYNTHETIC_FACTS_DB']
from ducky import mutation_journal as j
real_flock = j.fcntl.flock
def observed_flock(fd, operation):
    if operation == j.fcntl.LOCK_EX:
        pathlib.Path(os.environ['SYNTHETIC_MARKER']).write_text('attempting scope lock')
    return real_flock(fd, operation)
j.fcntl.flock = observed_flock
result = j.startup_recover()
pathlib.Path(os.environ['SYNTHETIC_RESULT']).write_text(json.dumps(result))
'''
    import time
    with journal.scope_lock('u', 'b'):
        mid = journal._insert('mem0_add', 'u', 'b', {}, state='started')
        env = {**os.environ, 'SYNTHETIC_FACTS_DB': str(utils.FACTS_DB),
               'SYNTHETIC_MARKER': str(marker), 'SYNTHETIC_RESULT': str(output)}
        child_proc = subprocess.Popen([sys.executable, '-c', source], env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            assert marker.exists()
            assert child_proc.poll() is None and not output.exists()
            journal._ack(mid, {'results': []})
        except BaseException:
            child_proc.kill()
            child_proc.communicate(timeout=5)
            raise
    stdout, stderr = child_proc.communicate(timeout=10)
    assert child_proc.returncode == 0, stdout + stderr
    assert json.loads(output.read_text())['held_for_repair'] == 0


def test_request_crash_after_sdk_ack_needs_repair_not_replay(isolated):
    source = '''
import os
from ducky import utils
utils.FACTS_DB = os.environ['SYNTHETIC_FACTS_DB']
from ducky import mutation_journal as j
def pipeline():
    j.perform_mutation('mem0_add', 'u', 'b', {'source':'full original'}, lambda: {'results': [{'id': 'persisted'}]})
    os._exit(75)
j.execute_request('u', 'b', {'messages':'full original'}, pipeline, request_key='stable')
'''
    result = subprocess.run([sys.executable, '-c', source],
                            env={**os.environ, 'SYNTHETIC_FACTS_DB': str(utils.FACTS_DB)},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 75, result.stderr
    journal.startup_recover()
    from ducky import idempotency
    receipt = idempotency.claim('stable', 'u', 'b', {'messages': 'full original'})
    assert receipt['action'] == 'pending' and receipt['reason'] == 'repair_required'
    assert {r['kind']: r['state'] for r in rows()} == {'request': 'repair_required', 'mem0_add': 'acknowledged'}


def test_repaired_then_forgotten_key_never_resurrects(isolated):
    from ducky import idempotency
    payload = {'messages': 'original'}
    journal.execute_request('u', 'b', payload, lambda: {'status': 'ok'}, request_key='stable')
    journal.erase_scope('u', 'b')
    receipt = idempotency.claim('stable', 'u', 'b', payload)
    assert receipt['action'] == 'replay' and receipt['response']['status'] == 'forgotten'
    assert receipt['response']['durable'] is False


def test_json_error_response_and_unknown_schema_never_report_success(isolated):
    with pytest.raises(journal.MutationUncertain):
        journal.perform_mutation('mem0_add', 'u', 'b', {}, lambda: {'status': 'partial'})
    with sqlite3.connect(journal.journal_path()) as conn:
        conn.execute('PRAGMA user_version=999')
    assert journal.journal_health()['status'] == 'degraded'
    with pytest.raises(journal.JournalEvidenceError) as error:
        journal.startup_recover()
    assert error.value.reason == 'journal_schema_unknown'


def test_target_erasure_preserves_content_free_receipt_of_unrelated_ack(isolated):
    from ducky import idempotency
    payload = {'messages': 'ERASE_EMBEDDED_MENTION_OF_OTHER_TARGET'}
    journal.execute_request('u', 'b', payload,
        lambda: {'status': 'ok', 'id': 'unrelated-id'}, request_key='other-request')
    journal.erase_target('u', 'b', 'deleted-id')
    receipt = idempotency.claim('other-request', 'u', 'b', payload)
    assert receipt['action'] == 'replay'
    assert receipt['response']['status'] == 'ok' and receipt['response']['durable'] is True
    assert b'ERASE_EMBEDDED_MENTION_OF_OTHER_TARGET' not in journal.journal_path().read_bytes()


def test_confirmed_applied_has_truthful_replay_receipt_without_original_response(isolated):
    from ducky import idempotency
    payload = {'messages': 'original'}
    with pytest.raises(journal.MutationUncertain):
        journal.execute_request('u', 'b', payload,
            lambda: (_ for _ in ()).throw(OSError('response missing')), request_key='stable')
    mid = rows()[0]['id']
    with pytest.raises(ValueError):
        journal.resolve_mutation(mid, 'u', 'b', resolution='confirmed_applied',
                                 evidence='verified', result={'status': 'failed'})
    journal.resolve_mutation(mid, 'u', 'b', resolution='confirmed_applied',
                             evidence='independent backend verification')
    receipt = idempotency.claim('stable', 'u', 'b', payload)
    assert receipt['response']['status'] == 'ok'
    assert receipt['response']['action'] == 'operator_verified'


def test_coalescing_job_keeps_legacy_status_after_memory_cache_loss(isolated):
    from ducky.speed import jobs
    jid = jobs.job_create({'user_id': 'u', 'bank_id': 'b', 'messages': 'original'})
    jobs.job_update(jid, status='coalescing', result={'status': 'coalescing', 'count': 1})
    jobs._jobs.clear()
    assert jobs.job_get(jid)['status'] == 'coalescing'
