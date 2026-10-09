"""Real HTTP + real mem0/Qdrant/SQLite crash probe, entirely synthetic/offline."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys

from f04_durability_probe import offline_memory
from f04_probe_guard import write_audit

BODY = {"messages": [{"role": "user", "content": "synthetic durable route input"}],
        "user_id": "synthetic-owner", "bank_id": "work", "infer": False,
        "idempotency_key": "route-stable-key", "metadata": {"no_coalesce": True}}


def isolate(root):
    root.mkdir(exist_ok=True)
    (root / 'tmp').mkdir(exist_ok=True)
    os.environ.update(AIDUMEM_DATA_DIR=str(root / 'data'), AIDUMEM_LOG_DIR=str(root / 'logs'),
                      MEM0_TELEMETRY='false', MEM0_DIR=str(root / 'sdk-state'),
                      HF_HOME=str(root / 'hf-cache'), XDG_CACHE_HOME=str(root / 'cache'),
                      TMPDIR=str(root / 'tmp'), AIDUMEI_ENGINE_MODE='cloud',
                      AIDUMEI_RATE_ADD_PER_MIN='0', AIDUMEI_RATE_ADD_GLOBAL_PER_MIN='0')
    sys.dont_write_bytecode = True
    real_makedirs = os.makedirs
    def makedirs(path, mode=0o777, exist_ok=False):
        if str(path) == '/tmp/qdrant':
            path = root / 'unused-qdrant-default'
        return real_makedirs(path, mode=mode, exist_ok=exist_ok)
    os.makedirs = makedirs
    sys.addaudithook(write_audit(root, 'isolated HTTP probe'))


def build_app(root):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from ducky.hot import add, crud
    from ducky import add_speed, gear, mem0_runtime
    from ducky.speed import coalesce
    memory = offline_memory(root / 'sdk')
    # Match the real startup schema prerequisites without starting daemons.
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.text_fts import _ensure_trigram_fts
    from ducky.utils import get_text_conn
    ensure_core_schema()
    conn = get_text_conn()
    try:
        _ensure_trigram_fts(conn)
        conn.commit()
    finally:
        conn.close()
    add.get_memory = crud.get_memory = mem0_runtime.get_memory = lambda: memory
    add_speed.ensure_coalesce_worker = coalesce.ensure_coalesce_worker = lambda: None
    gear.reset_gear_for_tests()
    app = FastAPI()
    add.register_add_routes(app)
    crud.register_crud_routes(app)
    return TestClient(app, raise_server_exceptions=False), memory


def records():
    from ducky import mutation_journal as journal
    with journal._db() as conn:
        return [dict(row) for row in conn.execute('SELECT * FROM mutations ORDER BY created_at')]


def dump(root, mode, client, memory, response=None, extra=None):
    from ducky import mutation_journal as journal, utils
    raw = []
    with sqlite3.connect(utils.FACTS_DB) as conn:
        if conn.execute("SELECT name FROM sqlite_master WHERE name='verbatim_turns'").fetchone():
            raw = conn.execute('SELECT content, occurrences FROM verbatim_turns').fetchall()
    out = {'http': response.status_code if response is not None else None,
           'response': response.json() if response is not None else None,
           'rows': records(), 'health': journal.journal_health(),
           'vectors': memory.get_all(filters={'user_id': BODY['user_id']})['results'],
           'raw': raw, **(extra or {})}
    (root / (mode + '.json')).write_text(json.dumps(out))
    memory.vector_store.client.close()


def race_batch_guard(root, client, memory, phase):
    """A competing recovery process must wait for the complete running→ACK span."""
    import subprocess
    import threading
    from ducky import mutation_journal as journal
    from ducky.hot import add
    entered, release = threading.Event(), threading.Event()
    original_update = journal.update_job
    original_pipeline = add._run_pipeline

    def update(jid, **fields):
        if phase == 'running' and fields.get('status') == 'running':
            original_update(jid, **fields)
            entered.set()
            assert release.wait(8)
            return
        if phase == 'done' and fields.get('status') == 'done':
            entered.set()
            assert release.wait(8)
        return original_update(jid, **fields)

    def pipeline(*args, **kwargs):
        result = original_pipeline(*args, **kwargs)
        if phase == 'callback':
            entered.set()
            assert release.wait(8)
        return result

    journal.update_job, add._run_pipeline = update, pipeline
    response = []
    thread = threading.Thread(target=lambda: response.append(client.post(
        '/add', json={**BODY, 'async_mode': True})))
    thread.start()
    assert entered.wait(8), 'batch did not reach guarded transition'
    attempt, complete = root / 'contender-attempt', root / 'contender-complete'
    code = ("from pathlib import Path; from ducky import mutation_journal as j; "
            "import sys; Path(sys.argv[1]).touch(); j.startup_recover(); Path(sys.argv[2]).touch()")
    proc = subprocess.Popen([sys.executable, '-c', code, str(attempt), str(complete)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    import time
    until = time.monotonic() + 8
    while not attempt.exists() and proc.poll() is None and time.monotonic() < until:
        time.sleep(.01)
    assert attempt.exists(), 'recovery process did not start'
    time.sleep(.15)
    blocked = not complete.exists()
    release.set()
    thread.join(8)
    stdout, stderr = proc.communicate(timeout=8)
    assert proc.returncode == 0, stdout + stderr
    assert not thread.is_alive()
    dump(root, 'guard_' + phase, client, memory, response[0], {'recovery_blocked': blocked})


def main():
    root, mode = Path(sys.argv[1]), sys.argv[2]
    isolate(root)
    client, memory = build_app(root)
    from ducky import mutation_journal as journal
    from ducky.hot import add
    if mode.startswith('guard_'):
        race_batch_guard(root, client, memory, mode.removeprefix('guard_'))
        return
    body = json.loads(json.dumps(BODY))
    if mode.startswith('retry'):
        recovered = journal.startup_recover()
        # Simulate legacy receipt expiry/loss; journal must remain authoritative.
        from ducky import utils
        with sqlite3.connect(utils.FACTS_DB) as conn:
            conn.execute('DELETE FROM idempotency_keys')
        if mode == 'retry_async':
            body['async_mode'] = True
        elif mode == 'retry_coalesce':
            body['async_mode'] = True
            body['metadata'] = {'no_fastpath': True}
        response = client.post('/add', json=body)
        dump(root, mode, client, memory, response, {'recovery': recovered})
        return
    if mode in ('sync_ack_exit', 'sync_ack_fail', 'sdk_ack_fail', 'local_ack_fail', 'lite_ack_fail'):
        target = 'mem0_add' if mode == 'sdk_ack_fail' else 'request'
        real_ack = journal._ack
        def ack(mid, result):
            if journal._get(mid)['kind'] == target:
                if mode == 'sync_ack_exit':
                    os._exit(81)
                raise sqlite3.OperationalError('synthetic receipt disk full')
            return real_ack(mid, result)
        journal._ack = ack
        if mode.startswith(('local', 'lite')):
            # Route selection only; cloud-mode local index is disabled so no
            # model download is possible. Actual SQL ingress/backlog still runs.
            add._write_mode = lambda: mode.split('_')[0]
    if mode in ('intent_fail', 'async_intent_fail'):
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError('synthetic intent disk full')
        journal._insert = fail
        if mode.startswith('async'):
            body['async_mode'] = True
    if mode in ('async_accept_exit', 'coalesce_accept_exit', 'async_done_fail', 'forgotten_callback'):
        body['async_mode'] = True
        if mode in ('coalesce_accept_exit', 'forgotten_callback'):
            body['metadata'] = {'no_fastpath': True}
        from fastapi import BackgroundTasks
        if mode != 'async_done_fail':
            async def defer(self):
                pass
            BackgroundTasks.__call__ = defer
        else:
            real_update = journal.update_job
            def update(jid, **fields):
                if fields.get('status') == 'done':
                    raise sqlite3.OperationalError('synthetic job ACK disk full')
                return real_update(jid, **fields)
            journal.update_job = update
    if mode in ('post_sdk_error', 'post_sdk_llm_error'):
        from ducky import layer1_selfcheck
        class LLMError(RuntimeError):
            pass
        def fail_after(*args, **kwargs):
            if mode == 'post_sdk_llm_error':
                raise LLMError('synthetic late error after real SDK write')
            raise RuntimeError('synthetic late index error after real SDK write')
        layer1_selfcheck._index_after_add = fail_after
    if mode == 'update_ack_fail':
        assert client.post('/add', json=body).status_code == 200
        target = memory.get_all(filters={'user_id': body['user_id']})['results'][0]['id']
        real_ack = journal._ack
        def ack(mid, result):
            if journal._get(mid)['kind'] == 'mem0_update':
                raise sqlite3.OperationalError('synthetic update ACK failure')
            return real_ack(mid, result)
        journal._ack = ack
        response = client.post('/update', json={'memory_id': target, 'content': 'synthetic changed content',
                                               'user_id': body['user_id'], 'bank_id': body['bank_id']})
    else:
        response = client.post('/add', json=body)
    extra = {}
    if mode == 'forgotten_callback':
        from ducky.speed import coalesce
        callback = coalesce._coalesce_flush_cb
        jid = response.json()['job_id']
        journal.erase_scope(body['user_id'], body['bank_id'])
        try:
            callback(body['user_id'], body['messages'], body['metadata'], [jid],
                     bank_id=body['bank_id'], infer=False)
        except journal.MutationUncertain:
            extra['stale_callback_blocked'] = True
    dump(root, mode, client, memory, response, extra)
    if mode.endswith('accept_exit'):
        os._exit(82)


if __name__ == '__main__':
    main()
