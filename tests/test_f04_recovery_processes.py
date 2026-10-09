"""Actual independent processes, real WAL/cascade/journal and SQLite backend."""
import json
import os
from pathlib import Path
import selectors
import sqlite3
import subprocess
import sys

import ducky.wal_engine as we


CHILD = r'''
import json, pathlib, sqlite3, sys
from types import SimpleNamespace
import ducky.wal_engine as we
import ducky.utils as utils
import ducky.mem0_runtime as rt
import ducky.dual_index as di
root = pathlib.Path(sys.argv[1]); role = sys.argv[2]
utils.FACTS_DB = str(root / 'facts.db')
utils.TEXT_FTS_DB = str(root / 'fts.db')
class Backend:
    def __init__(self):
        self.vector_store = self
        self.client = SimpleNamespace(get_collections=lambda: SimpleNamespace(collections=[]))
    def get_all(self, **kw):
        with sqlite3.connect(root / 'backend.db') as c:
            ids = [r[0] for r in c.execute('SELECT id FROM points')]
        return {'results': [{'id': i, 'user_id': 'alice', 'metadata': {'bank_id': 'work'}} for i in ids]}
    def delete(self, mid):
        with sqlite3.connect(root / 'backend.db') as c:
            c.execute('DELETE FROM points WHERE id=?', (mid,))
rt.get_memory = Backend
di.spawn_replay_daemon = lambda **kw: False
wal = we.WALEngine(str(root / 'wal')); we.WALEngine._instance = wal
real_pending = wal.get_pending_entries
read_once = False
def pending():
    global read_once
    rows = real_pending()
    if not read_once:
        read_once = True
        print('read:' + str(len(rows)), flush=True)
        if role == 'a': input()
    return rows
wal.get_pending_entries = pending
real_finish = we._finish_reconcile
def finish(report):
    result = real_finish(report)
    if role == 'a':
        # The first deletion is committed. A new write occurs before the
        # second process can acquire replay ownership (deterministic race).
        with sqlite3.connect(root / 'backend.db') as c:
            c.execute("INSERT INTO points VALUES('new-after-commit')")
    return result
we._finish_reconcile = finish
print('starting', flush=True)
print(json.dumps(we.reconcile_startup()), flush=True)
'''


def line(p):
    with selectors.DefaultSelector() as selector:
        selector.register(p.stdout, selectors.EVENT_READ)
        assert selector.select(15), 'child did not reach synchronization point'
    result = p.stdout.readline().decode().strip()
    assert result, p.stderr.read()
    return result


def test_two_processes_replay_delete_all_once_and_preserve_new_write(tmp_path, monkeypatch):
    import ducky.utils as utils
    from ducky.schema_bootstrap import ensure_core_schema
    from ducky.text_fts import _init_text_fts
    monkeypatch.setattr(utils, 'FACTS_DB', str(tmp_path / 'facts.db'))
    monkeypatch.setattr(utils, 'TEXT_FTS_DB', str(tmp_path / 'fts.db'))
    ensure_core_schema(force=True)
    _init_text_fts()
    with sqlite3.connect(tmp_path / 'backend.db') as c:
        c.execute('CREATE TABLE points(id TEXT PRIMARY KEY)')
        c.execute("INSERT INTO points VALUES('original')")
    wal = we.WALEngine(str(tmp_path / 'wal'))
    wal.append(we.WALEntry(wal_id='single-job', operation='delete_all', user_id='alice', bank_id='work', payload={'bank_id': 'work'}))
    processes = []
    try:
        for role in ('a', 'b'):
            p = subprocess.Popen([sys.executable, '-u', '-c', CHILD, str(tmp_path), role],
                                 env=os.environ.copy(), cwd=Path(__file__).resolve().parents[1],
                                 # Raw pipes keep selector readiness aligned with
                                 # unread lines; TextIOWrapper may read ahead.
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
            processes.append(p)
            assert line(p) == 'starting'
            if role == 'a':
                assert line(p) == 'read:1'
        a, b = processes
        with selectors.DefaultSelector() as sel:
            sel.register(b.stdout, selectors.EVENT_READ)
            assert not sel.select(.25), 'second process read a stale candidate list'
        a.stdin.write(b'continue\n'); a.stdin.flush()
        out_a, err_a = a.communicate(timeout=25)
        out_b, err_b = b.communicate(timeout=25)
        assert a.returncode == b.returncode == 0, (err_a, err_b)
        assert json.loads(out_a.splitlines()[-1])['recovered'] == 1
        assert out_b.splitlines()[0] == b'read:0'
        assert json.loads(out_b.splitlines()[-1])['recovered'] == 0
        with sqlite3.connect(tmp_path / 'backend.db') as c:
            assert c.execute('SELECT id FROM points').fetchall() == [('new-after-commit',)]
        assert wal.get_pending_entries() == []
    finally:
        for p in processes:
            if p.poll() is None:
                p.kill()
            p.communicate(timeout=10)


def test_killed_owner_releases_lock_without_losing_intent(tmp_path):
    wal = we.WALEngine(str(tmp_path / 'wal'))
    wal.append(we.WALEntry(wal_id='survives', operation='delete', payload={'memory_id': 'm'}))
    script = '''import sys
from ducky.wal_engine import WALEngine, mutation_ownership
w = WALEngine(sys.argv[1])
with mutation_ownership(w):
 print('owned', flush=True)
 input()
'''
    p = subprocess.Popen([sys.executable, '-u', '-c', script, str(wal.wal_dir)],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)
    try:
        assert line(p) == 'owned'
        p.kill(); p.communicate(timeout=10)
        with we.mutation_ownership(wal):
            assert [e.wal_id for e in wal.get_pending_entries()] == ['survives']
    finally:
        if p.poll() is None:
            p.kill(); p.communicate(timeout=10)
